"""Exact fractional inventory, prices, exports and the existing material workflows."""
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from threading import Barrier
import io

from openpyxl import load_workbook
import pytest

from app import db
from app.materials import claim_statistics, sold_totals, selection_total_cents
from app.models import AuditLog, MaterialClaim, MaterialItem, MaterialPurchase
from app.quantities import amount_cents, parse_quantity
from tests.test_materials import CODE, materials, session_as, purchase, claim, photo


@pytest.mark.parametrize("raw,expected", [
    ("0.5", "1/2"), (".5", "1/2"), ("1.25", "5/4"), ("1/3", "1/3"), ("2/3", "2/3"),
    ("1 1/2", "3/2"), ("2 2/3", "8/3"), (" 1 / 3 ", "1/3"), ("2/6", "1/3"),
    ("1.000000001", "1000000001/1000000000"), ("1000000", "1000000"), ("000.50", "1/2"),
])
def test_parse_fractional_quantity(raw, expected):
    assert parse_quantity(raw) == Fraction(expected)


@pytest.mark.parametrize("raw", [
    "", "0", "0.0", "0/3", "-1", "+1", "-1/3", "1/-3", "1/0", "1 3/2", "1 0/2", "1 1/0",
    "1/2/3", "1.5/3", "1又1/2", "NaN", "Infinity", "1e-3", "1,5", "1.", "１２", "²", "١/٣",
    "1000000.1", "0.0000000001", "1/1000000001", "1\n1/2", "9" * 65,
])
def test_invalid_fractional_quantity(raw):
    assert parse_quantity(raw) is None


def test_zero_for_stock_and_correction_only():
    assert parse_quantity("0", allow_zero=True) == 0
    assert parse_quantity("0/3", allow_zero=True) == 0
    assert parse_quantity("1.5", maximum=1) is None


@pytest.mark.parametrize("price,qty,expected", [(1, "1/2", 1), (1, "1/3", 0), (1, "2/3", 1),
    (235, "1/2", 118), (235, "1/3", 78), (101, "1/2", 51), (100, "1 1/2", 150), (100, "1.25", 125)])
def test_half_up_rounding(price, qty, expected):
    assert amount_cents(price, parse_quantity(qty)) == expected


def test_three_thirds_empty_one_unit_with_exact_statistics_and_excel(app, client, materials):
    with app.app_context():
        item = db.session.get(MaterialItem, materials["sale"])
        item.stock = 1
        item.unit_price_cents = 100
        db.session.commit()
    session_as(client, materials["member_id"])
    for remaining in (Fraction(2, 3), Fraction(1, 3), Fraction(0)):
        pid = purchase(client, materials["sale"], "1/3")
        client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
        with app.app_context():
            item = db.session.get(MaterialItem, materials["sale"])
            assert item.stock == remaining
            assert item.stock_exact == str(remaining)
            assert db.session.get(MaterialPurchase, pid).total_cents == 33
    response = client.post("/materials/buy", data={f'qty_{materials["sale"]}': "1/3", "photo": photo(), "confirm": "1"})
    assert response.status_code == 200
    with app.app_context():
        assert MaterialPurchase.query.count() == 3
        assert sold_totals()[0]["quantity"] == 1
        assert sold_totals()[0]["amount_cents"] == 99
    session_as(client, materials["officer_id"])
    assert "0.99" in client.get("/materials/admin?tab=settle").get_data(as_text=True)
    workbook = load_workbook(io.BytesIO(client.get("/materials/admin/export").data))
    assert [row[8] for row in list(workbook["材料购买"].values)[1:]] == ["1/3"] * 3
    assert [row[10] for row in list(workbook["材料购买"].values)[1:]] == [0.33] * 3


def test_order_sums_rounded_lines_instead_of_rounding_combined_quantity(app, client, materials):
    with app.app_context():
        for mid in (materials["sale"], materials["empty"]):
            item = db.session.get(MaterialItem, mid)
            item.stock = 1
            item.unit_price_cents = 1
        db.session.commit()
        picked = [(db.session.get(MaterialItem, mid), Fraction(1, 2)) for mid in (materials["sale"], materials["empty"])]
        assert selection_total_cents(picked) == 2
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"], "0.5", **{f'qty_{materials["empty"]}': "1/2"})
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.total_cents == 2
        assert sum(line.subtotal_cents for line in row.items) == 2
        assert sum(row["amount_cents"] for row in sold_totals()) == 2


@pytest.mark.parametrize("raw", ["0.5", "1.25", "1/3", "2/3", "1 1/2"])
def test_buy_accepts_all_supported_formats(app, client, materials, raw):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"], raw)
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        line = db.session.get(MaterialPurchase, pid).items[0]
        assert line.quantity == parse_quantity(raw)
        assert line.quantity_exact == str(parse_quantity(raw))
        assert db.session.get(MaterialItem, materials["sale"]).stock == 10 - parse_quantity(raw)


@pytest.mark.parametrize("raw", ["1/0", "-1/3", "0.0", "0/3", "1 3/2", "10 1/3", "1/1000000001"])
def test_buy_rejects_invalid_or_excessive_fraction(app, client, materials, raw):
    session_as(client, materials["member_id"])
    response = client.post("/materials/buy", data={f'qty_{materials["sale"]}': raw, "photo": photo(), "confirm": "1"})
    assert response.status_code == 200
    with app.app_context():
        assert MaterialPurchase.query.count() == 0
        assert db.session.get(MaterialItem, materials["sale"]).stock == 10


def test_fractional_limits_and_confirm_time_shortage(app, client, materials):
    with app.app_context():
        item = db.session.get(MaterialItem, materials["sale"])
        item.stock = Fraction(2, 3)
        item.max_per_purchase = Fraction(1, 2)
        db.session.commit()
    session_as(client, materials["member_id"])
    client.post("/materials/buy", data={f'qty_{materials["sale"]}': "2/3", "photo": photo(), "confirm": "1"})
    with app.app_context(): assert MaterialPurchase.query.count() == 0
    pid = purchase(client, materials["sale"], "1/2")
    with app.app_context():
        db.session.get(MaterialItem, materials["sale"]).stock = Fraction(1, 3)
        db.session.commit()
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        assert db.session.get(MaterialPurchase, pid).status == "cancelled"
        assert db.session.get(MaterialItem, materials["sale"]).stock == Fraction(1, 3)


def test_admin_sets_fractional_inventory_and_free_allotment(app, client, materials):
    session_as(client, materials["admin_id"])
    form = {"name": "分张竹皮", "unit": "张", "stock": "1 1/2", "per_team_qty": "1/3", "enabled": "1"}
    assert client.post("/materials/settings/free/new", data=form).status_code == 302
    with app.app_context():
        item = MaterialItem.query.filter_by(name="分张竹皮").one()
        mid = item.id
        assert item.stock == Fraction(3, 2) and item.per_team_qty == Fraction(1, 3)
    form.update(stock="2/3", per_team_qty="0.5")
    client.post(f"/materials/settings/items/{mid}/update", data=form)
    with app.app_context():
        item = db.session.get(MaterialItem, mid)
        assert item.stock == Fraction(2, 3) and item.per_team_qty == Fraction(1, 2)
    form.update(name="分张售卖", unit_price="1.01", stock="3.5", max_per_purchase="1/3")
    client.post("/materials/settings/sale/new", data=form)
    with app.app_context():
        item = MaterialItem.query.filter_by(name="分张售卖").one()
        assert item.stock == Fraction(7, 2) and item.max_per_purchase == Fraction(1, 3)


def test_free_fractional_claims_statistics_correction_and_restock(app, client, materials):
    with app.app_context():
        item = db.session.get(MaterialItem, materials["free"])
        item.stock = 1
        item.per_team_qty = Fraction(1, 3)
        db.session.commit()
    for uid in (materials["member_id"], materials["other_id"]):
        session_as(client, uid)
        claim(client)
    with app.app_context():
        assert db.session.get(MaterialItem, materials["free"]).stock == Fraction(1, 3)
        assert claim_statistics()["totals"][0]["quantity"] == Fraction(2, 3)
        record = MaterialClaim.query.first()
        rid, lid = record.id, record.items[0].id
    session_as(client, materials["admin_id"])
    client.post(f"/materials/admin/claims/{rid}/edit", data={f"qty_{lid}": "0.5"})
    with app.app_context(): assert db.session.get(MaterialItem, materials["free"]).stock == Fraction(1, 6)
    client.post(f"/materials/admin/claims/{rid}/void")
    with app.app_context(): assert db.session.get(MaterialItem, materials["free"]).stock == Fraction(2, 3)
    workbook = load_workbook(io.BytesIO(client.get("/materials/admin/export").data))
    assert list(workbook["发放汇总"].values)[1][2] == "1/3"


def test_fractional_correction_reprices_and_deletion_restores_exact_stock(app, client, materials):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"], "1/3")
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context(): lid = db.session.get(MaterialPurchase, pid).items[0].id
    session_as(client, materials["admin_id"])
    client.post(f"/materials/admin/purchases/{pid}/edit", data={f"qty_{lid}": "0.5", f"price_{lid}": "1.01"})
    with app.app_context():
        assert db.session.get(MaterialPurchase, pid).total_cents == 51
        assert db.session.get(MaterialItem, materials["sale"]).stock == Fraction(19, 2)
        assert "1/3" in AuditLog.query.filter_by(action="material.purchase_edit").one().payload
    client.post(f"/materials/admin/purchases/{pid}/delete")
    with app.app_context(): assert db.session.get(MaterialItem, materials["sale"]).stock == 10


@pytest.mark.parametrize("same_order", [True, False])
def test_fractional_concurrent_purchases_no_overselling(app, materials, same_order):
    with app.app_context():
        db.session.get(MaterialItem, materials["sale"]).stock = Fraction(2, 3)
        db.session.commit()
    jobs = []
    for uid in (materials["member_id"], materials["other_id"]):
        client = app.test_client(); session_as(client, uid)
        jobs.append((client, purchase(client, materials["sale"], "1/2")))
    if same_order:
        other = app.test_client(); session_as(other, materials["member_id"])
        jobs[1] = other, jobs[0][1]
    barrier = Barrier(2)
    def confirm(job):
        client, pid = job
        barrier.wait(timeout=10)
        return client.post(f"/materials/buy/{pid}/verify", data={"code": CODE}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: assert list(pool.map(confirm, jobs)) == [302, 302]
    with app.app_context():
        assert MaterialPurchase.query.filter_by(status="confirmed").count() == 1
        assert db.session.get(MaterialItem, materials["sale"]).stock == Fraction(1, 6)


def test_fractional_concurrent_claims_no_overselling(app, materials):
    with app.app_context():
        item = db.session.get(MaterialItem, materials["free"])
        item.stock, item.per_team_qty = Fraction(1, 2), Fraction(1, 3)
        db.session.commit()
    clients = [app.test_client(), app.test_client()]
    for client, uid in zip(clients, (materials["member_id"], materials["other_id"])): session_as(client, uid)
    barrier = Barrier(2)
    def submit(client):
        barrier.wait(timeout=10)
        return claim(client).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: assert list(pool.map(submit, clients)) == [302, 302]
    with app.app_context():
        assert MaterialClaim.query.count() == 1
        assert db.session.get(MaterialItem, materials["free"]).stock == Fraction(1, 6)


def test_unchanged_accumulated_stock_can_be_saved(app, client, materials):
    remaining = 1 - Fraction(1, 999983) - Fraction(1, 999979)
    assert remaining.denominator > 1_000_000_000
    with app.app_context():
        db.session.get(MaterialItem, materials["sale"]).stock = remaining
        db.session.commit()
    session_as(client, materials["admin_id"])
    response = client.post(f'/materials/settings/items/{materials["sale"]}/update', data={
        "name": "修改名称", "spec": "2mm", "unit": "根", "stock": str(remaining), "unit_price": "2.35", "enabled": "1"})
    assert response.status_code == 302
    with app.app_context():
        item = db.session.get(MaterialItem, materials["sale"])
        assert item.name == "修改名称" and item.stock == remaining
