#!/usr/bin/env python3
"""仅在 data/testing/app.db 写入示例数据；重复运行保留已有账号密码。"""

from __future__ import annotations

import sys
from datetime import date, time, timedelta
from pathlib import Path

from flask import current_app

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_config import create_demo_app
from app import db  # noqa: E402
from app.models import Booking, DutyShift, Lab, ToolCatalog, ToolLoan, ToolLoanItem, User  # noqa: E402
from app.utils import apply_team_roster, today, write_minimal_png  # noqa: E402

def get_or_create_user(student_id: str, name: str, role: str) -> User:
    user = User.query.filter_by(student_id=student_id).first()
    if user is not None:
        return user
    user = User(student_id=student_id, name=name, role=role)
    db.session.add(user)
    if role == "member":
        user.set_password(student_id, require_change=True)
    else:
        from app.utils import generate_unique_staff_password
        user.set_password(generate_unique_staff_password(), require_change=True)
    db.session.flush()
    return user


def get_or_create_lab(name: str, capacity: int, open_start: time, open_end: time, contact_name: str | None = None, contact_phone: str | None = None) -> Lab:
    lab = Lab.query.filter_by(name=name).first()
    if lab is None:
        lab = Lab(name=name, capacity=capacity, open_start=open_start, open_end=open_end, enabled=True)
        db.session.add(lab)
        db.session.flush()
    if contact_name and not lab.contact_name:
        lab.contact_name = contact_name
    if contact_phone and not lab.contact_phone:
        lab.contact_phone = contact_phone
    return lab


def has_booking(lab: Lab, user: User, day: date, start: time, end: time) -> bool:
    return (
        Booking.query.filter_by(
            lab_id=lab.id, user_id=user.id, date=day, start_time=start, end_time=end
        ).first()
        is not None
    )


def add_booking(lab, user, day, start, end, purpose, people_count, status, reviewer=None, reason=None):
    if has_booking(lab, user, day, start, end):
        return
    booking = Booking(
        lab=lab,
        user=user,
        date=day,
        start_time=start,
        end_time=end,
        purpose=purpose,
        people_count=people_count,
        captain_phone="13800138000",
        captain_email=user.last_email or "captain@example.com",
        status=status,
        reject_reason=reason,
        reviewed_by_id=reviewer.id if reviewer and status in ("approved", "rejected") else None,
    )
    db.session.add(booking)


def main() -> int:
    app = create_demo_app()
    with app.app_context():
        admin = User.query.filter_by(student_id="admin").first()
        officer = get_or_create_user("officer01", "李管理员", "admin")
        officer.contact_phone = officer.contact_phone or "13700137001"
        if admin is not None:
            admin.contact_phone = admin.contact_phone or "13600136001"
        member_a = get_or_create_user("2021001", "张三", "member")
        member_b = get_or_create_user("2021002", "王五", "member")
        member_c = get_or_create_user("2021003", "赵六", "member")
        member_a.last_email = member_a.last_email or "zhangsan@example.com"
        member_b.last_email = member_b.last_email or "wangwu@example.com"
        member_c.last_email = member_c.last_email or "zhaoliu@example.com"
        member_a.last_phone = member_a.last_phone or "13800138000"
        member_b.last_phone = member_b.last_phone or "13800138002"
        member_c.last_phone = member_c.last_phone or "13800138003"
        apply_team_roster(
            member_a,
            "结构先锋队",
            [
                {"name": "李四", "student_id": "2021011"},
                {"name": "周七", "student_id": "2021012"},
            ],
        )
        apply_team_roster(
            member_b,
            "力学探索队",
            [{"name": "吴八", "student_id": "2021021"}],
        )
        apply_team_roster(
            member_c,
            "创新桥队",
            [{"name": "郑九", "student_id": "2021022"}],
        )

        lab_a = get_or_create_lab("创新实验室", 10, time(8, 0), time(22, 0), "创新实验室值班室", "13500135001")
        lab_b = get_or_create_lab("电子实验室", 8, time(9, 0), time(21, 0), "电子实验室值班室", "13500135002")
        lab_c = get_or_create_lab("机械车间", 6, time(8, 30), time(18, 0), "机械车间值班室", "13500135003")

        day = today() + timedelta(days=14)
        past = today() - timedelta(days=7)

        add_booking(lab_a, member_a, day, time(10, 0), time(11, 0), "社团例会准备", 4, "approved", reviewer=officer)
        add_booking(lab_a, member_b, day, time(11, 0), time(12, 0), "设备调试（与上一场首尾相接）", 2, "pending")
        add_booking(lab_a, member_c, day, time(14, 0), time(15, 0), "作品拍摄", 3, "pending")
        add_booking(lab_b, member_a, day, time(9, 30), time(10, 30), "电路焊接练习", 2, "pending")
        add_booking(lab_b, member_b, day + timedelta(days=1), time(13, 0), time(14, 0), "传感器实验", 3, "approved", reviewer=admin or officer)
        add_booking(lab_c, member_c, day, time(10, 0), time(11, 0), "结构件加工", 2, "rejected", reviewer=officer, reason="当天设备检修")
        add_booking(lab_a, member_a, past, time(15, 0), time(16, 0), "已完成的历史使用", 5, "approved", reviewer=officer)

        if DutyShift.query.count() == 0:
            db.session.add(
                DutyShift(
                    lab=lab_a,
                    admin=officer,
                    specific_date=day,
                    start_time=time(8, 0),
                    end_time=time(18, 0),
                )
            )
            db.session.add(
                DutyShift(
                    lab=lab_b,
                    admin=admin or officer,
                    weekday=day.weekday(),
                    start_time=time(9, 0),
                    end_time=time(17, 0),
                )
            )

        def get_or_create_tool(name, total, is_major=True):
            item = ToolCatalog.query.filter_by(name=name).first()
            if item is None:
                item = ToolCatalog(name=name, total_quantity=total, is_major=is_major)
                db.session.add(item)
                db.session.flush()
            else:
                item.total_quantity = total
                item.is_major = is_major
            return item

        tool_wrench = get_or_create_tool("扳手", 10)
        tool_caliper = get_or_create_tool("卡尺", 5)
        tool_meter = get_or_create_tool("万用表", 3)
        tool_hat = get_or_create_tool("安全帽", 8)

        upload_dir = Path(current_app.config["UPLOAD_DIR"])
        if ToolLoan.query.count() == 0:
            from app.utils import local_now

            now = local_now().replace(second=0, microsecond=0)
            photo_a = write_minimal_png(upload_dir / "seed_borrow_a.png").name
            photo_b = write_minimal_png(upload_dir / "seed_borrow_b.png").name
            photo_c = write_minimal_png(upload_dir / "seed_borrow_c.png").name
            photo_back = write_minimal_png(upload_dir / "seed_return_c.png").name

            loan_a = ToolLoan(
                user=member_a,
                lab=lab_a,
                team_name=member_a.team_name,
                contact="13800138000",
                borrow_at=now,
                expected_return_at=now + timedelta(days=1),
                borrow_photo=photo_a,
                status="borrowing",
            )
            db.session.add(loan_a)
            db.session.flush()
            db.session.add(ToolLoanItem(loan=loan_a, catalog_id=tool_wrench.id, name="扳手", quantity=2, is_other=False))
            db.session.add(ToolLoanItem(loan=loan_a, catalog_id=tool_caliper.id, name="卡尺", quantity=1, is_other=False))

            loan_b = ToolLoan(
                user=member_b,
                lab=lab_b,
                team_name=member_b.team_name,
                contact="13800138002",
                borrow_at=now - timedelta(days=3),
                expected_return_at=now - timedelta(days=1),
                borrow_photo=photo_b,
                status="borrowing",
            )
            db.session.add(loan_b)
            db.session.flush()
            db.session.add(ToolLoanItem(loan=loan_b, catalog_id=tool_meter.id, name="万用表", quantity=1, is_other=False))

            loan_c = ToolLoan(
                user=member_c,
                lab=lab_c,
                team_name=member_c.team_name,
                contact="13800138003",
                borrow_at=now - timedelta(days=2),
                expected_return_at=now - timedelta(days=1),
                actual_return_at=now - timedelta(hours=12),
                borrow_photo=photo_c,
                return_photo=photo_back,
                status="returned",
            )
            db.session.add(loan_c)
            db.session.flush()
            db.session.add(ToolLoanItem(loan=loan_c, catalog_id=tool_hat.id, name="安全帽", quantity=3, is_other=False))

        db.session.commit()
        print("示例数据已写入独立测试库 data/testing/app.db（已存在的同名实验室/同学号账号会被跳过）。")
        print("独立演示账号：officer01 使用随机管理员密码；参赛队长初始密码等于学号，首次登录须修改：")
        print("  officer01  李管理员  管理员")
        print("  2021001    张三    参赛队长（结构先锋队，队员李四/周七）\n  2021002    王五    参赛队长（力学探索队，队员吴八）\n  2021003    赵六    参赛队长（创新桥队，队员郑九）")
        print("如需已知测试管理员凭据，请运行 scripts/write_test_accounts.py；本脚本不会读取生产凭据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
