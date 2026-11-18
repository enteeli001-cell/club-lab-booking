"""Material workflows, authorization, accounting and concurrent SQLite writes."""
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from threading import Barrier

import pytest
from openpyxl import load_workbook

from app import db
from app import verification as vc
from app.models import (AuditLog, MailLog, MaterialClaim, MaterialItem,
                        MaterialPurchase, User)
from app.toggles import set_feature, set_maintenance
from app.utils import MINIMAL_PNG_BYTES, local_now

CODE = "314159"  # deterministic test-only verification code


def session_as(client, uid):
    with client.session_transaction() as session:
        session.clear()
        session["_user_id"] = str(uid)
        session["_fresh"] = True


def photo():
    return io.BytesIO(MINIMAL_PNG_BYTES), "materials.png"


@pytest.fixture
def materials(app, seed, monkeypatch):
    monkeypatch.setattr(vc, "generate_code", lambda: CODE)
    with app.app_context():
        for key in ("member_id", "other_id"):
            user = db.session.get(User, seed[key])
            user.team_name = "测试队" + str(user.id)
            user.last_phone = "13800138000"
            user.last_email = user.verified_email = f"captain{user.id}@example.com"
            user.email_verified_at = local_now()
        free = MaterialItem(kind="free", name="竹皮", spec="A1", unit="张", stock=10, per_team_qty=2)
        sale = MaterialItem(kind="sale", name="竹条", spec="2mm", unit="根", stock=10, unit_price_cents=235)
        empty = MaterialItem(kind="sale", name="胶水", unit="瓶", stock=0, unit_price_cents=1020)
        db.session.add_all([free, sale, empty])
        db.session.commit()
        return {**seed, "free": free.id, "sale": sale.id, "empty": empty.id}


def purchase(client, material_id, qty=2, **extra):
    response = client.post("/materials/buy", data={f"qty_{material_id}": str(qty),
                           "photo": photo(), "confirm": "1", **extra})
    assert response.status_code == 302
    assert response.location.endswith("/verify"), response.location
    return int(response.location.split("/")[-2])


def claim(client):
    return client.post("/materials/claim", data={"confirm": "1", "photo": photo()})


def test_claim_once_photo_statistics_and_export(app, client, materials):
    session_as(client, materials["member_id"])
    assert claim(client).status_code == 302
    assert claim(client).status_code == 302
    with app.app_context():
        row = MaterialClaim.query.one()
        assert row.items[0].quantity == 2 and row.ip == "127.0.0.1"
        assert row.created_at and row.team_name and row.user_id == materials["member_id"]
        filename = row.photo
        assert db.session.get(MaterialItem, materials["free"]).stock == 8
        assert AuditLog.query.filter_by(action="material.claim").count() == 1
    assert client.get(f"/materials/media/{filename}").status_code == 200
    session_as(client, materials["other_id"])
    assert client.get(f"/materials/media/{filename}").status_code == 403
    session_as(client, materials["officer_id"])
    assert client.get(f"/materials/media/{filename}").status_code == 200
    page = client.get("/materials/admin").get_data(as_text=True)
    assert "已领取" in page and "未领取" in page and "竹皮" in page
    response = client.get("/materials/admin/export")
    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.data))
    assert len(workbook.sheetnames) >= 4
    assert "竹皮" in str(list(workbook.worksheets[0].values))


@pytest.mark.parametrize("bad", ["missing_photo", "unconfirmed", "shortage", "empty"])
def test_claim_invalid_does_not_deduct(app, client, materials, bad):
    with app.app_context():
        item = db.session.get(MaterialItem, materials["free"])
        if bad == "shortage": item.stock = 1
        if bad == "empty": item.enabled = False
        db.session.commit()
    session_as(client, materials["member_id"])
    data = {"confirm": "1", "photo": photo()}
    if bad == "missing_photo": data.pop("photo")
    if bad == "unconfirmed": data.pop("confirm")
    client.post("/materials/claim", data=data)
    with app.app_context():
        assert MaterialClaim.query.count() == 0
        assert db.session.get(MaterialItem, materials["free"]).stock == (1 if bad == "shortage" else 10)


def test_purchase_recomputes_price_and_redacts_code(app, client, materials):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"], qty=3, total_cents="1", unit_price="0.01")
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.total_cents == 705
        assert row.code_hash and row.code_hash != CODE and len(row.code_hash) == 64
        assert row.email_to == row.user.verified_email
        assert db.session.get(MaterialItem, materials["sale"]).stock == 10
        mail = MailLog.query.filter_by(subject="材料购买验证码").one()
        assert CODE not in mail.body and "******" in mail.body
        from pathlib import Path
        message = BytesParser(policy=policy.default).parsebytes((Path(app.config["OUTBOX_DIR"]) / mail.filename).read_bytes())
        assert CODE not in message.get_body(preferencelist=("plain",)).get_content()
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.status == "confirmed" and row.total_cents == 705 and not row.settled
        assert row.confirmed_at and row.ip and row.photo and row.code_hash is None
        assert db.session.get(MaterialItem, materials["sale"]).stock == 7
        assert AuditLog.query.filter_by(action="material.purchase").count() == 1


@pytest.mark.parametrize("qty", ["-1", "1/0", "11", "NaN", "²", "9" * 100, "0"])
def test_invalid_quantity_rejected(app, client, materials, qty):
    session_as(client, materials["member_id"])
    assert client.post("/materials/buy", data={f'qty_{materials["sale"]}': qty, "photo": photo(), "confirm": "1"}).status_code == 200
    with app.app_context():
        assert MaterialPurchase.query.count() == 0
        assert db.session.get(MaterialItem, materials["sale"]).stock == 10


def test_soldout_and_limit_cannot_be_forged(app, client, materials):
    session_as(client, materials["member_id"])
    with app.app_context():
        db.session.get(MaterialItem, materials["sale"]).max_per_purchase = 1
        db.session.commit()
    for mid, qty in [(materials["empty"], 1), (materials["sale"], 2)]:
        client.post("/materials/buy", data={f"qty_{mid}": qty, "photo": photo(), "confirm": "1"})
    with app.app_context(): assert MaterialPurchase.query.count() == 0


@pytest.mark.parametrize("case", ["expired", "wrong_limit", "price_changed", "stock_changed", "disabled", "email_changed"])
def test_confirmation_revalidates(app, client, materials, case):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"])
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        item = db.session.get(MaterialItem, materials["sale"])
        if case == "expired": row.code_expires_at = local_now() - timedelta(seconds=1)
        if case == "stock_changed": item.stock = 1
        if case == "price_changed": item.unit_price_cents += 1
        if case == "disabled": item.enabled = False
        if case == "email_changed": row.user.verified_email = "new@example.com"
        db.session.commit()
    if case == "wrong_limit":
        for _ in range(5): client.post(f"/materials/buy/{pid}/verify", data={"code": "000000"})
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.status != "confirmed"
        assert db.session.get(MaterialItem, materials["sale"]).stock == (1 if case == "stock_changed" else 10)
        if case == "wrong_limit": assert row.code_attempts == 5 and row.code_hash is None


def test_multiline_purchase_rolls_back_every_stock(app, client, materials):
    with app.app_context():
        db.session.get(MaterialItem, materials["empty"]).stock = 3
        db.session.commit()
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"], **{f'qty_{materials["empty"]}': "2"})
    with app.app_context():
        db.session.get(MaterialItem, materials["empty"]).stock = 1
        db.session.commit()
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        assert db.session.get(MaterialItem, materials["sale"]).stock == 10
        assert db.session.get(MaterialPurchase, pid).status == "cancelled"


def test_stock_failure_keeps_writer_lock_until_cancellation(app, client, materials):
    import sqlite3
    from app.materials import confirm_purchase
    from app.transactions import begin_serialized_write
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"])
    with app.app_context():
        db.session.get(MaterialItem, materials["sale"]).stock = 0
        db.session.commit()
        begin_serialized_write()
        row = db.session.get(MaterialPurchase, pid)
        assert confirm_purchase(row)
        with sqlite3.connect(app.config["DB_PATH"], timeout=0) as other_writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other_writer.execute("UPDATE material_items SET stock = stock + 1")
        row.status = "cancelled"
        db.session.commit()


def test_resend_cooldown_rotation_and_cap(app, client, materials, monkeypatch):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"])
    client.post(f"/materials/buy/{pid}/resend")
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.code_send_count == 1
        row.code_sent_at = local_now() - timedelta(seconds=61)
        old_hash = row.code_hash
        db.session.commit()
    monkeypatch.setattr(vc, "generate_code", lambda: "271828")
    client.post(f"/materials/buy/{pid}/resend")
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.code_send_count == 2 and row.code_hash != old_hash
        row.code_sent_at = local_now() - timedelta(seconds=61)
        row.code_send_count = 5
        db.session.commit()
    client.post(f"/materials/buy/{pid}/resend")
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    with app.app_context():
        assert db.session.get(MaterialPurchase, pid).code_send_count == 5
        assert db.session.get(MaterialPurchase, pid).status == "pending"


@pytest.mark.parametrize("role", ["member_id", "officer_id", "other_id"])
def test_mutation_permissions(app, client, materials, role):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"])
    claim(client)
    with app.app_context(): cid = MaterialClaim.query.one().id
    session_as(client, materials[role])
    for path in ["/materials/settings/free/new", f'/materials/settings/items/{materials["sale"]}/update',
                 f'/materials/settings/items/{materials["sale"]}/delete', f"/materials/admin/claims/{cid}/edit",
                 f"/materials/admin/claims/{cid}/void", f"/materials/admin/claims/{cid}/delete",
                 f"/materials/admin/purchases/{pid}/edit", f"/materials/admin/purchases/{pid}/settle",
                 f"/materials/admin/purchases/{pid}/void", f"/materials/admin/purchases/{pid}/delete",
                 f'/materials/admin/teams/{materials["member_id"]}/settle']:
        assert client.post(path).status_code == 403, path
    for path in ["/materials/admin", "/materials/admin/export", "/profile/people"]:
        assert client.get(path).status_code == (200 if role == "officer_id" else 403)


def test_other_captain_cannot_verify_or_cancel(app, client, materials):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"])
    session_as(client, materials["other_id"])
    for action in ("verify", "resend", "cancel"):
        client.post(f"/materials/buy/{pid}/{action}", data={"code": CODE})
    with app.app_context():
        assert db.session.get(MaterialPurchase, pid).status == "pending"
        assert db.session.get(MaterialPurchase, pid).code_send_count == 1


@pytest.mark.parametrize("key,path", [("material_claim", "/materials/claim"), ("material_purchase", "/materials/buy")])
def test_feature_switches_and_maintenance(app, client, materials, key, path):
    with app.app_context(): set_feature(key, False)
    session_as(client, materials["member_id"])
    assert client.get(path).status_code == 403
    assert client.post(path).status_code == 403
    if key == "material_purchase":
        for suffix in ("verify", "resend", "cancel"):
            assert client.post(f"/materials/buy/1/{suffix}").status_code == 403
    with app.app_context(): set_maintenance(True)
    assert client.get(path).status_code == 503
    session_as(client, materials["officer_id"])
    assert client.get("/materials/admin").status_code == 503
    session_as(client, materials["admin_id"])
    assert client.get("/materials/settings").status_code == 200


def test_superadmin_correct_settle_void_delete_with_audit(app, client, materials):
    session_as(client, materials["member_id"])
    pid = purchase(client, materials["sale"], 3)
    client.post(f"/materials/buy/{pid}/verify", data={"code": CODE})
    claim(client)
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        lid = row.items[0].id
        cid = MaterialClaim.query.one().id
    session_as(client, materials["admin_id"])
    client.post(f'/materials/admin/teams/{materials["member_id"]}/settle')
    client.post(f"/materials/admin/purchases/{pid}/edit", data={f"qty_{lid}": "4", f"price_{lid}": "1.25", "note": "更正数量和价格"})
    with app.app_context():
        row = db.session.get(MaterialPurchase, pid)
        assert row.total_cents == 500 and not row.settled
        assert db.session.get(MaterialItem, materials["sale"]).stock == 6
    client.post(f"/materials/admin/purchases/{pid}/settle", data={"settled": "1"})
    assert "5.00" in client.get("/materials/admin?tab=settle").get_data(as_text=True)
    client.post(f"/materials/admin/purchases/{pid}/void")
    client.post(f"/materials/admin/purchases/{pid}/void")
    client.post(f"/materials/admin/purchases/{pid}/delete")
    client.post(f"/materials/admin/claims/{cid}/delete")
    with app.app_context():
        assert MaterialPurchase.query.count() == MaterialClaim.query.count() == 0
        assert db.session.get(MaterialItem, materials["sale"]).stock == 10
        assert db.session.get(MaterialItem, materials["free"]).stock == 10
        actions = {a.action for a in AuditLog.query.all()}
        assert {"material.purchase_edit", "material.settle", "material.settle_team", "material.purchase_void", "material.purchase_delete", "material.claim_delete"} <= actions


def test_free_allotment_locks_after_first_claim(app, client, materials):
    session_as(client, materials["member_id"])
    claim(client)
    session_as(client, materials["admin_id"])
    fields = {"name": "竹皮", "spec": "A1", "unit": "张", "stock": "20", "per_team_qty": "3", "enabled": "1"}
    client.post(f'/materials/settings/items/{materials["free"]}/update', data=fields)
    client.post(f'/materials/settings/items/{materials["free"]}/delete')
    client.post("/materials/settings/free/new", data=fields)
    with app.app_context():
        item = db.session.get(MaterialItem, materials["free"])
        assert item and item.per_team_qty == 2 and item.stock == 8
    fields["per_team_qty"] = "2"
    client.post(f'/materials/settings/items/{materials["free"]}/update', data=fields)
    with app.app_context(): assert db.session.get(MaterialItem, materials["free"]).stock == 20


@pytest.mark.parametrize("same_order", [False, True])
def test_concurrent_confirmations_do_not_oversell(app, materials, same_order):
    with app.app_context():
        db.session.get(MaterialItem, materials["sale"]).stock = 1
        db.session.commit()
    jobs = []
    for uid in (materials["member_id"], materials["other_id"]):
        client = app.test_client(); session_as(client, uid)
        jobs.append((client, purchase(client, materials["sale"], 1)))
    if same_order:
        client = app.test_client(); session_as(client, materials["member_id"])
        jobs[1] = (client, jobs[0][1])
    barrier = Barrier(2)
    def confirm(job):
        client, pid = job
        barrier.wait(timeout=10)
        return client.post(f"/materials/buy/{pid}/verify", data={"code": CODE}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(confirm, jobs)) == [302, 302]
    with app.app_context():
        assert db.session.get(MaterialItem, materials["sale"]).stock == 0
        assert MaterialPurchase.query.filter_by(status="confirmed").count() == 1


@pytest.mark.parametrize("same_team", [False, True])
def test_concurrent_claims_do_not_duplicate_or_oversell(app, materials, same_team):
    with app.app_context():
        db.session.get(MaterialItem, materials["free"]).stock = 2
        db.session.commit()
    clients = [app.test_client(), app.test_client()]
    session_as(clients[0], materials["member_id"])
    session_as(clients[1], materials["member_id"] if same_team else materials["other_id"])
    barrier = Barrier(2)
    def submit(client):
        barrier.wait(timeout=10)
        return claim(client).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: assert list(pool.map(submit, clients)) == [302, 302]
    with app.app_context():
        assert db.session.get(MaterialItem, materials["free"]).stock == 0
        assert MaterialClaim.query.count() == 1


@pytest.mark.parametrize("raw,expected", [("0", 0), (0, 0), ("12.35", 1235), ("1.001", None), ("-1", None), ("NaN", None), ("Infinity", None), ("1e999999999", None), ("10000.01", None)])
def test_price_parser_bounds(raw, expected):
    from app.materials import parse_price_cents
    assert parse_price_cents(raw) == expected
