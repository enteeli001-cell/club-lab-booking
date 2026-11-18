"""Announcement board, forced popups, level-3 mail round, footer text and review pagination."""
import re
from datetime import date, datetime, time, timedelta

import pytest

from app import db
from app.announcements import scan_announcement_mail
from app.jobs import run_scans
from app.models import (
    Announcement,
    AnnouncementAck,
    AnnouncementMail,
    AnnouncementRead,
    AuditLog,
    Booking,
    MailLog,
    User,
)
from tests.conftest import FUTURE_DAY, create_lab, create_user, login

OTHER_ADMIN_PASSWORD = "OtherAdm#34pq"


@pytest.fixture
def world(app, seed):
    """seed + a second admin and mailable captains (example.com only)."""
    with app.app_context():
        officer2 = create_user("officer02", "赵管理员", "admin", OTHER_ADMIN_PASSWORD)
        member = db.session.get(User, seed["member_id"])
        other = db.session.get(User, seed["other_id"])
        member.last_email = "zhangsan@example.com"
        other.last_email = "wangwu@example.com"
        db.session.commit()
        return {**seed, "officer2_id": officer2.id}


def _staff(app, who="officer01"):
    client = app.test_client()
    password = {"admin": None, "officer01": None, "officer02": OTHER_ADMIN_PASSWORD}[who]
    login(client, who, password, staff=True)
    return client


def _captain(app, student_id="2021001"):
    client = app.test_client()
    login(client, student_id)
    return client


def _publish(client, title="实验室周末开放", body="周六 9:00-17:00 开放。", **extra):
    return client.post("/announcements/new", data={"publisher_name": "测试发布人", "title": title, "body": body, **extra})


def _by_title(title):
    return Announcement.query.filter_by(title=title).one()


def _aid(app, title):
    with app.app_context():
        return _by_title(title).id


# ------------------------------------------------------------------ permissions

def test_admin_can_only_publish_level_one_even_if_forging_the_form(app, world):
    admin = _staff(app)
    resp = _publish(admin, "想弹窗", level="3", pinned="1")
    assert resp.status_code == 302
    with app.app_context():
        a = _by_title("想弹窗")
        assert (a.level, a.pinned, a.mail_revision) == (1, False, 0)
        assert a.created_by_id == world["officer_id"]


def test_admin_cannot_see_edit_delete_or_read_report_of_other_admins_announcements(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "总管理员的公告")
    mine_other = _staff(app, "officer02")
    _publish(mine_other, "赵的公告")
    mine = _staff(app)
    _publish(mine, "李的公告")
    theirs, boss_id = _aid(app, "赵的公告"), _aid(app, "总管理员的公告")
    own = _aid(app, "李的公告")

    page = mine.get("/announcements/manage").get_data(as_text=True)
    assert "李的公告" in page and "赵的公告" not in page and "总管理员的公告" not in page
    # The public list still shows every announcement.
    listing = mine.get("/announcements/").get_data(as_text=True)
    assert "李的公告" in listing and "赵的公告" in listing and "总管理员的公告" in listing

    for target in (theirs, boss_id):
        assert mine.get(f"/announcements/{target}/reads").status_code == 403
        assert mine.get(f"/announcements/{target}/edit").status_code == 403
        assert mine.post(f"/announcements/{target}/edit", data={"publisher_name": "测试发布人", "title": "x", "body": "y"}).status_code == 403
        assert mine.post(f"/announcements/{target}/delete").status_code == 403
    # Detail page of someone else's announcement has no manage links or read data.
    detail = mine.get(f"/announcements/{theirs}").get_data(as_text=True)
    assert "阅读情况" not in detail and "删除" not in detail
    with app.app_context():
        assert Announcement.query.count() == 3 and _by_title("赵的公告").title == "赵的公告"
    # Own announcement works.
    assert mine.get(f"/announcements/{own}/reads").status_code == 200
    assert mine.post(f"/announcements/{own}/edit", data={"publisher_name": "测试发布人", "title": "李的公告2", "body": "改"}).status_code == 302


def test_captain_cannot_publish_or_manage(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "公告A")
    aid = _aid(app, "公告A")
    cap = _captain(app)
    for url in ("/announcements/new", "/announcements/manage", f"/announcements/{aid}/edit", f"/announcements/{aid}/reads"):
        assert cap.get(url).status_code == 403
    assert cap.post("/announcements/new", data={"publisher_name": "测试发布人", "title": "x", "body": "y"}).status_code == 403
    assert cap.post(f"/announcements/{aid}/delete").status_code == 403
    assert cap.get(f"/announcements/{aid}").status_code == 200  # reading is allowed


def test_superadmin_sees_edits_and_deletes_everything_with_audit_log(app, world):
    admin = _staff(app)
    _publish(admin, "李的公告")
    aid = _aid(app, "李的公告")
    boss = _staff(app, "admin")
    assert "李的公告" in boss.get("/announcements/manage").get_data(as_text=True)
    assert boss.get(f"/announcements/{aid}/reads").status_code == 200
    resp = boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "李的公告（改）", "body": "总管理员改过", "level": "2", "pinned": "1"})
    assert resp.status_code == 302
    with app.app_context():
        a = db.session.get(Announcement, aid)
        assert a.level == 2 and a.pinned and a.updated_by_id == world["admin_id"] and a.created_by_id == world["officer_id"]
    assert boss.post(f"/announcements/{aid}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(Announcement, aid) is None
        actions = [row.action for row in AuditLog.query.order_by(AuditLog.id).all() if row.action.startswith("announcement.")]
        assert actions == ["announcement.create", "announcement.update", "announcement.delete"]
        assert "他人发布的" in AuditLog.query.filter_by(action="announcement.delete").one().summary


def test_ack_requires_csrf(app, world):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    # Log in through the app internals; the point is only that a bare POST is rejected.
    with client.session_transaction() as sess:
        sess["_user_id"] = str(world["member_id"])
        sess["_fresh"] = True
    assert client.post("/announcements/ack", data={"items": "1:1"}).status_code == 400


# ------------------------------------------------------------------ reads

def test_read_is_recorded_once_with_the_first_time(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "读一次")
    aid = _aid(app, "读一次")
    cap = _captain(app)
    cap.get(f"/announcements/{aid}")
    with app.app_context():
        first = AnnouncementRead.query.one().read_at
        AnnouncementRead.query.one().read_at = first - timedelta(hours=3)
        db.session.commit()
        first = AnnouncementRead.query.one().read_at
    cap.get(f"/announcements/{aid}")
    cap.get(f"/announcements/{aid}")
    with app.app_context():
        assert AnnouncementRead.query.count() == 1
        assert AnnouncementRead.query.one().read_at == first
    # Another user is counted separately.
    _captain(app, "2021002").get(f"/announcements/{aid}")
    with app.app_context():
        assert AnnouncementRead.query.count() == 2


def test_reads_report_shows_names_ids_times_and_counts_for_owner(app, world):
    admin = _staff(app)
    _publish(admin, "统计我的")
    aid = _aid(app, "统计我的")
    _captain(app).get(f"/announcements/{aid}")
    page = admin.get(f"/announcements/{aid}/reads").get_data(as_text=True)
    assert "已读 <strong>1</strong> / 应读 <strong>2</strong>" in page
    assert "张三" in page and "2021001" in page and "王五" in page
    assert "张三" in admin.get(f"/announcements/{aid}/reads?show=read").get_data(as_text=True)
    unread = admin.get(f"/announcements/{aid}/reads?show=unread").get_data(as_text=True)
    assert "王五" in unread and "张三" not in unread
    manage = admin.get("/announcements/manage").get_data(as_text=True)
    assert "1 / 2" in manage


def test_should_read_counts_only_active_captains(app, world):
    with app.app_context():
        db.session.get(User, world["other_id"]).enabled = False
        create_user("2021003", "特批甲", "approved_member")
        db.session.commit()
    admin = _staff(app)
    _publish(admin, "应读")
    page = admin.get(f"/announcements/{_aid(app, '应读')}/reads").get_data(as_text=True)
    assert "应读 <strong>2</strong>" in page and "王五" not in page and "特批甲" in page


# ------------------------------------------------------------------ list / display

def test_list_pins_first_then_newest_and_shows_publisher_and_latest_editor(app, world):
    admin = _staff(app)
    boss = _staff(app, "admin")
    _publish(admin, "旧的一级", publisher_name="李管理员")
    _publish(admin, "新的一级", publisher_name="李管理员")
    _publish(boss, "置顶的二级", level="2", pinned="1", publisher_name="总管理员")
    with app.app_context():
        for title, days in (("旧的一级", 5), ("新的一级", 1), ("置顶的二级", 9)):
            a = _by_title(title)
            a.created_at = a.created_at - timedelta(days=days)
        db.session.commit()
    cap = _captain(app)
    page = cap.get("/announcements/").get_data(as_text=True)
    assert page.index("置顶的二级") < page.index("新的一级") < page.index("旧的一级")
    assert "发布人：李管理员" in page and "发布人：总管理员" in page
    assert "最近修改" not in page
    # The superadmin edits the admin's notice: publisher stays, latest editor is shown.
    aid = _aid(app, "旧的一级")
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "旧的一级", "body": "改了正文", "level": "1"} | {"publisher_name": "李管理员"})
    for url in ("/announcements/", f"/announcements/{aid}"):
        text = cap.get(url).get_data(as_text=True)
        assert "发布人：李管理员" in text
        assert "最近修改：总管理员（总管理员）" in text


def test_nav_puts_announcements_between_workbench_and_booking(app, world):
    cap = _captain(app)
    html = cap.get("/captain").get_data(as_text=True)
    nav = html[html.index('id="site-nav"'):]
    assert nav.index(">工作台<") < nav.index(">公告") < nav.index(">测试预约<")
    admin = _staff(app)
    nav = admin.get("/").get_data(as_text=True)
    nav = nav[nav.index('id="site-nav"'):]
    assert nav.index(">管理工作台<") < nav.index(">公告")


def test_unread_badge_counts_until_opened(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "红点")
    cap = _captain(app)
    assert 'aria-label="未读公告 1"' in cap.get("/captain").get_data(as_text=True)
    cap.get(f"/announcements/{_aid(app, '红点')}")
    assert "未读公告" not in cap.get("/captain").get_data(as_text=True)


# ------------------------------------------------------------------ popups

def test_level_one_never_pops_up(app, world):
    _publish(_staff(app), "普通")
    assert "announce-overlay" not in _captain(app).get("/captain").get_data(as_text=True)


def test_level_two_pops_up_until_acknowledged_and_ack_is_stored(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "二级弹窗", level="2", body="请注意安全。")
    cap = _captain(app)
    html = cap.get("/captain").get_data(as_text=True)
    assert "announce-overlay" in html and "二级弹窗" in html and "我已知晓" in html and "请注意安全。" in html
    # Pops up on any page, for admins too (but not for the author).
    assert "announce-overlay" in cap.get("/announcements/").get_data(as_text=True)
    assert "announce-overlay" in _staff(app).get("/").get_data(as_text=True)
    assert "announce-overlay" not in boss.get("/").get_data(as_text=True)
    # Not pinned unless asked.
    with app.app_context():
        a = _by_title("二级弹窗")
        assert a.pinned is False
        aid, rev = a.id, a.ack_revision
    resp = cap.post("/announcements/ack", data={"items": f"{aid}:{rev}", "next": "/captain"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/captain")
    assert "announce-overlay" not in cap.get("/captain").get_data(as_text=True)
    with app.app_context():
        ack = AnnouncementAck.query.one()
        assert ack.user_id == world["member_id"] and ack.revision == rev and ack.acked_at is not None
        assert AnnouncementRead.query.filter_by(user_id=world["member_id"]).count() == 1
        assert AuditLog.query.filter_by(action="announcement.ack").count() == 1
    # Still shown to the other captain.
    assert "announce-overlay" in _captain(app, "2021002").get("/captain").get_data(as_text=True)
    # Acknowledging twice does not duplicate.
    cap.post("/announcements/ack", data={"items": f"{aid}:{rev}"})
    with app.app_context():
        assert AnnouncementAck.query.count() == 1


def test_ack_ignores_garbage_and_open_redirects(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "二级", level="2")
    cap = _captain(app)
    resp = cap.post("/announcements/ack", data={"items": ["zzz", "999:1", "1;2"], "next": "//evil.example"})
    assert resp.status_code == 302 and "evil.example" not in resp.headers["Location"]
    with app.app_context():
        assert AnnouncementAck.query.count() == 0


def test_popups_ordered_by_level_then_newest_and_pin_option(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "二级旧", level="2", pinned="1")
    _publish(boss, "二级新", level="2")
    _publish(boss, "三级", level="3")
    with app.app_context():
        _by_title("二级旧").created_at -= timedelta(days=2)
        assert _by_title("二级旧").pinned and not _by_title("二级新").pinned
        db.session.commit()
    html = _captain(app).get("/captain").get_data(as_text=True)
    overlay = html[html.index("announce-overlay"):html.index("<main")]
    assert overlay.index("三级") < overlay.index("二级新") < overlay.index("二级旧")


def test_reconfirm_option_pops_up_again_and_plain_edit_does_not(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "会修改", level="2")
    aid = _aid(app, "会修改")
    cap = _captain(app)
    cap.post("/announcements/ack", data={"items": f"{aid}:1"})
    assert "announce-overlay" not in cap.get("/captain").get_data(as_text=True)
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "会修改", "body": "小改", "level": "2"})
    assert "announce-overlay" not in cap.get("/captain").get_data(as_text=True)
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "会修改", "body": "大改", "level": "2", "reack": "1"})
    html = cap.get("/captain").get_data(as_text=True)
    assert "announce-overlay" in html and "大改" in html
    with app.app_context():
        assert db.session.get(Announcement, aid).ack_revision == 2
    # A stale popup (revision 1) must not cover the new revision.
    cap.post("/announcements/ack", data={"items": f"{aid}:1"})
    assert "announce-overlay" in cap.get("/captain").get_data(as_text=True)
    cap.post("/announcements/ack", data={"items": f"{aid}:2"})
    assert "announce-overlay" not in cap.get("/captain").get_data(as_text=True)


def test_admin_cannot_use_reconfirm_or_change_level_when_editing_own(app, world):
    boss = _staff(app, "admin")
    admin = _staff(app)
    _publish(admin, "李的")
    aid = _aid(app, "李的")
    admin.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "李的", "body": "改", "level": "3", "pinned": "1", "reack": "1", "resend": "1"})
    with app.app_context():
        a = db.session.get(Announcement, aid)
        assert (a.level, a.pinned, a.ack_revision, a.mail_revision) == (1, False, 1, 0)
    # After the superadmin raises it, the admin still cannot lower it or force a resend.
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "李的", "body": "改", "level": "3"})
    admin.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "李的", "body": "再改", "level": "1", "reack": "1", "resend": "1"})
    with app.app_context():
        a = db.session.get(Announcement, aid)
        assert (a.level, a.pinned, a.ack_revision, a.mail_revision) == (3, True, 1, 1)


def test_no_popup_while_forced_password_change_is_pending(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "弹窗", level="2")
    with app.app_context():
        create_user("2021009", "新队长", "member", password="2021009")
    cap = app.test_client()
    html = login(cap, "2021009", "2021009").get_data(as_text=True)
    assert "announce-overlay" not in html


# ------------------------------------------------------------------ level 3 mail

def test_level_three_is_pinned_and_mails_everyone_once_without_blocking_publish(app, world):
    boss = _staff(app, "admin")
    resp = _publish(boss, "紧急通知", level="3", body="明天停电。")
    assert resp.status_code == 302
    with app.app_context():
        a = _by_title("紧急通知")
        assert a.pinned and a.level == 3 and a.mail_revision == 1
        assert MailLog.query.count() == 0  # nothing is sent inside the publish request
        result = scan_announcement_mail()
        assert result == {"sent": 2, "failed": 0, "skipped_no_email": 0}
        logs = MailLog.query.order_by(MailLog.recipient).all()
        assert [m.recipient for m in logs] == ["wangwu@example.com", "zhangsan@example.com"]
        assert all(m.status == "local" and "紧急通知" in m.subject and "明天停电。" in m.body for m in logs)
        assert {m.status for m in AnnouncementMail.query.all()} == {"local"}
        # Idempotent: scanning again (or through run_scans) sends nothing more.
        assert scan_announcement_mail() == {"sent": 0, "failed": 0, "skipped_no_email": 0}
        assert run_scans()["announcements"] == {"sent": 0, "failed": 0, "skipped_no_email": 0}
        assert MailLog.query.count() == 2
        assert AnnouncementMail.query.count() == 2


def test_level_three_email_choice_staff_excluded_and_no_email_flagged(app, world):
    with app.app_context():
        create_lab("二号")
        db.session.get(User, world["other_id"]).last_email = None
        # 王五 has no account email but a booking-form email.
        lab = create_lab("三号")
        db.session.add(Booking(lab_id=lab.id, user_id=world["other_id"], date=FUTURE_DAY, start_time=time(9), end_time=time(10),
                               purpose="p", people_count=1, captain_email="form-wangwu@example.com"))
        create_user("2021003", "无邮箱", "special_guest")
        db.session.commit()
    boss = _staff(app, "admin")
    _publish(boss, "三级", level="3")
    with app.app_context():
        assert scan_announcement_mail() == {"sent": 2, "failed": 0, "skipped_no_email": 1}
        assert sorted(m.recipient for m in MailLog.query.all()) == ["form-wangwu@example.com", "zhangsan@example.com"]
        row = AnnouncementMail.query.filter_by(user_id=User.query.filter_by(student_id="2021003").one().id).one()
        assert row.status == "no_email"
        assert "admin" not in " ".join(m.recipient for m in MailLog.query.all())
    page = boss.get(f"/announcements/{_aid(app, '三级')}/reads").get_data(as_text=True)
    assert "无邮箱" in page


def test_level_three_failed_mail_retries_at_most_twice(app, world, monkeypatch):
    calls = []

    class _Log:
        status = "failed"

    def fake_send(recipient, subject, body, *, booking_id=None):
        calls.append(recipient)
        return _Log()

    monkeypatch.setattr("app.mailer.send_mail", fake_send)
    boss = _staff(app, "admin")
    _publish(boss, "会失败", level="3")
    with app.app_context():
        results = [scan_announcement_mail() for _ in range(5)]
        assert [r["failed"] for r in results] == [2, 2, 2, 0, 0]
        assert len(calls) == 6  # 2 recipients x (first try + 2 retries)
        assert {m.attempts for m in AnnouncementMail.query.all()} == {3}
        assert _by_title("会失败").mail_done_revision == 1


def test_level_three_mail_recovers_when_a_retry_succeeds(app, world, monkeypatch):
    from app import mailer

    real = mailer.send_mail
    state = {"n": 0}

    class _Log:
        status = "failed"

    def flaky(recipient, subject, body, *, booking_id=None):
        state["n"] += 1
        if state["n"] <= 2:
            return _Log()
        return real(recipient, subject, body, booking_id=booking_id)

    monkeypatch.setattr("app.mailer.send_mail", flaky)
    _publish(_staff(app, "admin"), "先失败后成功", level="3")
    with app.app_context():
        assert scan_announcement_mail()["failed"] == 2
        assert scan_announcement_mail() == {"sent": 2, "failed": 0, "skipped_no_email": 0}
        assert scan_announcement_mail() == {"sent": 0, "failed": 0, "skipped_no_email": 0}
        assert {m.status for m in AnnouncementMail.query.all()} == {"local"}


def test_editing_level_three_does_not_resend_unless_asked(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "改邮件", level="3")
    aid = _aid(app, "改邮件")
    with app.app_context():
        scan_announcement_mail()
        assert MailLog.query.count() == 2
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "改邮件", "body": "改了正文", "level": "3"})
    with app.app_context():
        assert scan_announcement_mail()["sent"] == 0 and MailLog.query.count() == 2
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "改邮件", "body": "又改了", "level": "3", "resend": "1"})
    with app.app_context():
        assert scan_announcement_mail() == {"sent": 2, "failed": 0, "skipped_no_email": 0}
        assert MailLog.query.count() == 4
        assert "又改了" in MailLog.query.order_by(MailLog.id.desc()).first().body
        assert scan_announcement_mail()["sent"] == 0 and MailLog.query.count() == 4


def test_raising_to_level_three_mails_once_and_lower_levels_never_mail(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "一级", level="1")
    _publish(boss, "二级", level="2")
    with app.app_context():
        assert scan_announcement_mail()["sent"] == 0
    aid = _aid(app, "二级")
    boss.post(f"/announcements/{aid}/edit", data={"publisher_name": "测试发布人", "title": "二级", "body": "x", "level": "3"})
    with app.app_context():
        assert scan_announcement_mail()["sent"] == 2
        assert db.session.get(Announcement, aid).pinned


def test_delete_removes_reads_acks_and_mail_rows(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "将删除", level="3")
    aid = _aid(app, "将删除")
    cap = _captain(app)
    cap.post("/announcements/ack", data={"items": f"{aid}:1"})
    with app.app_context():
        scan_announcement_mail()
        assert AnnouncementRead.query.count() == 1 and AnnouncementAck.query.count() == 1 and AnnouncementMail.query.count() == 2
    boss.post(f"/announcements/{aid}/delete")
    with app.app_context():
        assert AnnouncementRead.query.count() == AnnouncementAck.query.count() == AnnouncementMail.query.count() == 0
    assert "announce-overlay" not in cap.get("/captain").get_data(as_text=True)


def test_form_validation_and_xss_escaping(app, world):
    boss = _staff(app, "admin")
    assert _publish(boss, "", "").status_code == 200
    assert _publish(boss, "t" * 101, "b").status_code == 200
    assert _publish(boss, "ok", "b", level="9").status_code == 200
    with app.app_context():
        assert Announcement.query.count() == 0
    _publish(boss, "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", level="2")
    html = _captain(app).get("/captain").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html


# ------------------------------------------------------------------ footer

def test_footer_has_no_organization_notice(app, world):
    import app.notices as notices

    assert not hasattr(notices, "FOOTER_NOTICE")
    assert "请勿外传" not in app.test_client().get("/login").get_data(as_text=True)
    assert "site-footer" in _staff(app, "admin").get("/announcements/").get_data(as_text=True)
    assert "某某大学" not in _captain(app).get("/captain").get_data(as_text=True)


# ------------------------------------------------------------------ review pagination

@pytest.fixture
def many_bookings(app, world):
    with app.app_context():
        lab = create_lab("大实验室", capacity=50)
        base_day = date(2099, 8, 1)
        counts = {"pending": 120, "approved": 60, "rejected": 10}
        n = 0
        for status, count in counts.items():
            for i in range(count):
                n += 1
                db.session.add(Booking(
                    lab_id=lab.id, user_id=world["member_id"], date=base_day + timedelta(days=n % 28),
                    start_time=time(8 + n % 10, 0), end_time=time(9 + n % 10, 0), purpose=f"p{n}", people_count=1,
                    status=status, created_at=datetime(2099, 7, 1) + timedelta(minutes=n),
                    checked_in_at=(datetime(2099, 8, 1, 9) if status == "approved" and i < 20 else None),
                ))
        db.session.commit()
    return counts


def _rows(html):
    return html.count('<td>2021001') + html.count("2021001<div")


def test_review_pagination_50_per_page_for_every_status(app, many_bookings):
    admin = _staff(app)
    expected = {"pending": (120, 3), "approved": (60, 2), "rejected": (10, 1), "all": (190, 4)}
    for status, (total, pages) in expected.items():
        first = admin.get(f"/review/?status={status}").get_data(as_text=True)
        assert f"共 <strong>{total}</strong> 条" in first and f"第 1 / {pages} 页" in first, status
        assert first.count("<tr>") - 1 == min(50, total), status  # minus the header row
        last = admin.get(f"/review/?status={status}&page={pages}").get_data(as_text=True)
        assert last.count("<tr>") - 1 == total - 50 * (pages - 1), status
        assert ('rel="next"' in first) == (pages > 1)


def test_review_pagination_pages_do_not_overlap_and_follow_ordering(app, many_bookings):
    import re

    admin = _staff(app)
    seen = []
    for page in (1, 2, 3):
        html = admin.get(f"/review/?status=pending&page={page}").get_data(as_text=True)
        seen += re.findall(r"/review/(\d+)/approve", html)
    assert len(seen) == 120 and len(set(seen)) == 120
    # Newest created first: ids were inserted oldest-first.
    assert [int(x) for x in seen] == sorted((int(x) for x in seen), reverse=True)


def test_review_default_page_bad_page_and_filters_are_kept(app, many_bookings):
    admin = _staff(app)
    default = admin.get("/review/").get_data(as_text=True)
    assert "共 <strong>120</strong> 条" in default and "第 1 / 3 页" in default
    for bad in ("0", "-4", "abc", ""):
        assert admin.get(f"/review/?page={bad}").status_code == 200
    clamped = admin.get("/review/?status=pending&page=99").get_data(as_text=True)
    assert "第 3 / 3 页" in clamped
    missing = admin.get("/review/?status=approved&checkin=missing").get_data(as_text=True)
    assert "共 <strong>40</strong> 条" in missing and "第 1 / 1 页" in missing
    done = admin.get("/review/?status=approved&checkin=done&page=1").get_data(as_text=True)
    assert "共 <strong>20</strong> 条" in done
    big = admin.get("/review/?status=all").get_data(as_text=True)
    assert "status=all" in big and "page=2" in big
    assert 'rel="next"' not in missing  # a single page has no next link


def test_review_next_link_keeps_status_and_checkin(app, many_bookings):
    import re

    with app.app_context():
        for b in Booking.query.filter_by(status="approved").all():
            b.checked_in_at = None  # 60 approved + never checked in -> two pages
        db.session.commit()
    admin = _staff(app)
    html = admin.get("/review/?status=approved&checkin=missing").get_data(as_text=True)
    link = re.search(r'<a[^>]*rel="next"[^>]*href="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert "page=2" in link and "status=approved" in link and "checkin=missing" in link
    second = admin.get(link).get_data(as_text=True)
    assert "第 2 / 2 页" in second and 'rel="prev"' in second


def test_review_actions_still_work_with_pagination(app, many_bookings):
    admin = _staff(app)
    html = admin.get("/review/?status=pending&page=3").get_data(as_text=True)
    import re

    ids = re.findall(r"/review/(\d+)/approve", html)
    assert ids
    with app.app_context():  # the generated bookings overlap each other; give the two under test free slots
        for offset, booking_id in enumerate(ids[:2]):
            db.session.get(Booking, int(booking_id)).date = date(2099, 12, 1 + offset)
        db.session.commit()
    resp = admin.post(f"/review/{ids[0]}/approve", follow_redirects=True)
    assert "已通过 张三 的预约" in resp.get_data(as_text=True)
    assert admin.post(f"/review/{ids[1]}/reject", data={"reason": "不合适"}).status_code == 302
    with app.app_context():
        assert db.session.get(Booking, int(ids[0])).status == "approved"
        assert db.session.get(Booking, int(ids[1])).status == "rejected"
    after = admin.get("/review/").get_data(as_text=True)
    assert "共 <strong>118</strong> 条" in after


def test_review_conflict_hint_still_shown_on_a_page(app, world):
    with app.app_context():
        lab = create_lab("冲突室")
        for uid in (world["member_id"], world["other_id"]):
            db.session.add(Booking(lab_id=lab.id, user_id=uid, date=FUTURE_DAY, start_time=time(10), end_time=time(11),
                                   purpose="p", people_count=1, status="pending"))
        db.session.commit()
    html = _staff(app).get("/review/").get_data(as_text=True)
    assert html.count("与其他待审核预约重叠超过 30 分钟") == 2


# ------------------------------------------------------------------ editable publisher name

def test_publisher_name_defaults_to_current_user_and_is_required_in_the_form(app, world):
    admin = _staff(app)
    html = admin.get("/announcements/new").get_data(as_text=True)
    assert re.search(r'<input id="publisher_name" name="publisher_name" type="text" maxlength="30" required value="李管理员">', html)
    assert "<label for=\"publisher_name\">发布人</label>" in html
    boss = _staff(app, "admin")
    assert 'value="总管理员"' in boss.get("/announcements/new").get_data(as_text=True).split('id="publisher_name"')[1][:140]


def test_publisher_name_can_be_set_shown_everywhere_and_edited(app, world):
    admin = _staff(app)
    _publish(admin, "有发布人", level="1", publisher_name="  结构工程协会  秘书处 ")
    aid = _aid(app, "有发布人")
    with app.app_context():
        a = db.session.get(Announcement, aid)
        assert a.publisher_name == "结构工程协会 秘书处"  # stripped, inner whitespace collapsed
        assert a.created_by_id == world["officer_id"]
    cap = _captain(app)
    for url in ("/announcements/", f"/announcements/{aid}"):
        text = cap.get(url).get_data(as_text=True)
        assert "发布人：结构工程协会 秘书处" in text and "实际账号" not in text and "李管理员" not in text
    # Edit form shows the saved value; editing changes it, "最近修改" stays the real account.
    assert 'value="结构工程协会 秘书处"' in admin.get(f"/announcements/{aid}/edit").get_data(as_text=True)
    admin.post(f"/announcements/{aid}/edit", data={"title": "有发布人", "body": "x", "publisher_name": "新名字"})
    with app.app_context():
        a = db.session.get(Announcement, aid)
        assert a.publisher_name == "新名字" and a.updated_by_id == world["officer_id"]
        audit = AuditLog.query.filter_by(action="announcement.update").one()
        assert "结构工程协会 秘书处" in audit.summary and "新名字" in audit.summary
    text = cap.get(f"/announcements/{aid}").get_data(as_text=True)
    assert "发布人：新名字" in text and "最近修改：李管理员（管理员）" in text
    # The manage tables: admin sees the name, superadmin additionally the real account in small grey text.
    am = admin.get("/announcements/manage").get_data(as_text=True)
    assert "新名字" in am and "实际账号" not in am
    boss = _staff(app, "admin")
    manage = boss.get("/announcements/manage").get_data(as_text=True)
    assert "新名字" in manage and "实际账号：李管理员（officer01）" in manage and 'class="hint account-real"' in manage
    assert "实际账号：李管理员" in boss.get(f"/announcements/{aid}").get_data(as_text=True)
    assert "新名字" in admin.get(f"/announcements/{aid}/reads").get_data(as_text=True)


def test_publisher_name_required_max_30_and_form_keeps_input(app, world):
    admin = _staff(app)
    missing = _publish(admin, "无发布人", publisher_name="   ")
    assert missing.status_code == 200 and "请填写发布人。" in missing.get_data(as_text=True)
    long = _publish(admin, "太长", body="保留这段正文", publisher_name="长" * 31)
    html = long.get_data(as_text=True)
    assert long.status_code == 200 and "发布人不能超过 30 个字。" in html and "保留这段正文" in html
    with app.app_context():
        assert Announcement.query.count() == 0
    ok = _publish(admin, "刚好30", publisher_name="长" * 30)
    assert ok.status_code == 302
    aid = _aid(app, "刚好30")
    bad = admin.post(f"/announcements/{aid}/edit", data={"title": "刚好30", "body": "x", "publisher_name": ""})
    assert bad.status_code == 200 and "请填写发布人。" in bad.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Announcement, aid).publisher_name == "长" * 30


def test_publisher_name_is_not_an_identity_admin_posing_as_superadmin_gains_nothing(app, world):
    boss = _staff(app, "admin")
    mine_other = _staff(app, "officer02")
    _publish(mine_other, "赵的公告", publisher_name="赵管理员")
    _publish(boss, "总管理员的公告", publisher_name="总管理员")
    theirs, bosses = _aid(app, "赵的公告"), _aid(app, "总管理员的公告")
    posing = _staff(app)
    _publish(posing, "冒名公告", publisher_name="总管理员", level="3", pinned="1")
    with app.app_context():
        a = _by_title("冒名公告")
        assert a.publisher_display == "总管理员" and a.created_by_id == world["officer_id"]
        assert (a.level, a.pinned) == (1, False)
    for target in (theirs, bosses):
        assert posing.get(f"/announcements/{target}/edit").status_code == 403
        assert posing.post(f"/announcements/{target}/edit", data={"title": "x", "body": "y", "publisher_name": "总管理员"}).status_code == 403
        assert posing.post(f"/announcements/{target}/delete").status_code == 403
        assert posing.get(f"/announcements/{target}/reads").status_code == 403
    # The real account decides what shows up in "my announcements".
    manage = posing.get("/announcements/manage").get_data(as_text=True)
    assert "冒名公告" in manage and "赵的公告" not in manage and "总管理员的公告" not in manage
    # A superadmin may rename anyone's publisher; permissions are untouched for the owner.
    own = _aid(app, "冒名公告")
    boss.post(f"/announcements/{own}/edit", data={"title": "冒名公告", "body": "x", "publisher_name": "真实管理员", "level": "1"})
    with app.app_context():
        a = db.session.get(Announcement, own)
        assert a.publisher_name == "真实管理员" and a.updated_by_id == world["admin_id"] and a.created_by_id == world["officer_id"]
    assert posing.get(f"/announcements/{own}/edit").status_code == 200


def test_self_popup_exclusion_follows_the_real_author_not_the_name(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "弹窗公告", level="2", publisher_name="某某协会")
    other_super_view = _captain(app).get("/captain").get_data(as_text=True)
    assert "某某协会" in other_super_view and "announce-overlay" in other_super_view
    assert "announce-overlay" not in boss.get("/").get_data(as_text=True)  # the real author never gets the popup
    # A different admin whose display name equals the publisher name still gets it.
    _publish(_staff(app), "名字一样", level="1", publisher_name="某某协会")
    assert "announce-overlay" in _staff(app).get("/").get_data(as_text=True)


def test_level_three_email_uses_publisher_name(app, world):
    boss = _staff(app, "admin")
    _publish(boss, "邮件公告", level="3", publisher_name="某某协会")
    with app.app_context():
        scan_announcement_mail()
        body = MailLog.query.first().body
        assert "发布人：某某协会" in body and "总管理员（总管理员）" not in body


def test_legacy_announcement_without_publisher_name_falls_back_to_author(app, world):
    boss = _staff(app, "admin")
    with app.app_context():
        db.session.add(Announcement(title="旧公告", body="x", level=1, created_by_id=world["officer_id"]))
        db.session.commit()
    aid = _aid(app, "旧公告")
    assert "发布人：李管理员" in _captain(app).get(f"/announcements/{aid}").get_data(as_text=True)
    assert 'value="李管理员"' in boss.get(f"/announcements/{aid}/edit").get_data(as_text=True).split('id="publisher_name"')[1][:140]


def test_publisher_name_column_is_added_incrementally_to_an_existing_database(app, world):
    from sqlalchemy import inspect, text

    from app import _ensure_announcement_schema

    with app.app_context():
        db.session.add(Announcement(title="旧数据", body="保留", level=2, created_by_id=world["officer_id"], publisher_name="x"))
        db.session.commit()
        db.session.execute(text("ALTER TABLE announcements DROP COLUMN publisher_name"))
        db.session.commit()
        assert "publisher_name" not in {c["name"] for c in inspect(db.engine).get_columns("announcements")}
        _ensure_announcement_schema()
        _ensure_announcement_schema()  # idempotent
        assert "publisher_name" in {c["name"] for c in inspect(db.engine).get_columns("announcements")}
        row = Announcement.query.one()
        assert (row.title, row.body, row.level, row.publisher_name) == ("旧数据", "保留", 2, None)
        assert row.publisher_display == "李管理员"
