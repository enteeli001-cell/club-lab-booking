from datetime import time
from io import BytesIO

from openpyxl import load_workbook

from app import db
from app.models import User
from tests.conftest import FUTURE_DAY, add_booking, login


def test_member_cannot_export(app, client, seed):
    login(client, "2021001")
    assert client.get("/stats/").status_code == 403
    assert client.get("/stats/export").status_code == 403


def test_stats_and_excel_export(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        other = db.session.get(User, seed["other_id"])
        add_booking(lab, member, start=time(10, 0), end=time(11, 30), status="approved", purpose="通过A")
        add_booking(lab, other, start=time(14, 0), end=time(15, 0), status="approved", purpose="通过B")
        add_booking(lab, member, start=time(16, 0), end=time(17, 0), status="pending", purpose="不应计入")

    login(client, "officer01", staff=True)
    page = client.get(f"/stats/?start_date={FUTURE_DAY.isoformat()}&end_date={FUTURE_DAY.isoformat()}")
    assert page.status_code == 200
    assert "2 次".encode("utf-8") in page.data or "2".encode("utf-8") in page.data
    assert "2小时30分钟".encode("utf-8") in page.data

    resp = client.get(f"/stats/export?start_date={FUTURE_DAY.isoformat()}&end_date={FUTURE_DAY.isoformat()}")
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    wb = load_workbook(BytesIO(resp.data))
    assert set(wb.sheetnames) >= {"按实验室", "按成员", "明细"}
    lab_sheet = wb["按实验室"]
    # header + 1 lab + total
    values = [row[1].value for row in lab_sheet.iter_rows(min_row=2, max_col=2)]
    assert 2 in values
    minutes = [row[2].value for row in lab_sheet.iter_rows(min_row=2, max_col=3)]
    assert 150 in minutes
    detail = wb["明细"]
    purposes = [row[8].value for row in detail.iter_rows(min_row=2, max_col=9)]
    assert "通过A" in purposes and "通过B" in purposes
    assert "不应计入" not in purposes
    user_sheet = wb["按成员"]
    counts = {row[1].value: row[2].value for row in user_sheet.iter_rows(min_row=2, max_col=3) if row[1].value}
    assert counts["2021001"] == 1
    assert counts["2021002"] == 1
