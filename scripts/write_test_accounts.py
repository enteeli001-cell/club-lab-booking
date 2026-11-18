#!/usr/bin/env python3
"""Create TEST001 and testadmin only in data/testing, preserving existing passwords.

Captain initial password equals its demo student id and requires a first-login change.
Test admin gets a generated password stored only in the isolated, gitignored directory.
No password is ever printed, and reruns preserve existing passwords.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_config import DEMO_DATA, create_demo_app

ACCOUNTS_FILE = DEMO_DATA / "test_accounts.txt"


def main() -> int:
    from app import db
    from app.models import User
    from app.utils import apply_team_roster, generate_unique_staff_password

    app = create_demo_app()
    with app.app_context():
        captain = User.query.filter_by(student_id="TEST001").first()
        if captain is None:
            captain = User(student_id="TEST001", name="测试队长", role="member", enabled=True)
            captain.set_password(captain.student_id, require_change=True)
            db.session.add(captain)
            db.session.flush()
        captain.name = "测试队长"
        captain.role = "member"
        captain.enabled = True
        captain.last_email = "test-captain@example.com"
        captain.last_phone = "13900001001"
        apply_team_roster(
            captain,
            "测试队",
            [
                {"name": "测试队员甲", "student_id": "TEST011", "phone": "13900001011"},
                {"name": "测试队员乙", "student_id": "TEST012", "phone": "13900001012"},
            ],
        )

        admin = User.query.filter_by(student_id="testadmin").first()
        admin_password = None
        if admin is None:
            admin_password = generate_unique_staff_password()
            admin = User(student_id="testadmin", name="测试管理员", role="admin", enabled=True)
            admin.set_password(admin_password, require_change=True)
            db.session.add(admin)
            db.session.flush()
        admin.name = "测试管理员"
        admin.role = "admin"
        admin.enabled = True
        admin.contact_phone = admin.contact_phone or "13900002001"
        db.session.commit()

    text = (
        "测试账号（请妥善保管，勿对外公开）\n"
        "\n"
        "测试队长\n"
        "  学号：TEST001\n"
        "  姓名：测试队长\n"
        "  队伍：测试队\n"
        "  登录入口：普通登录（学号+密码）\n"
        "  初始密码：等于学号；首次登录须改密\n"
        "\n"
        "测试管理员\n"
        "  学号：testadmin\n"
        "  姓名：测试管理员\n"
        "  登录入口：管理员登录（只输入密码）\n"
        "  密码：" + (admin_password or "保留原密码（本次未重设）") + "\n"
        "\n"
        "这些账号仅存在于 data/testing/app.db，与生产账号完全独立。\n"
    )
    if admin_password is None and ACCOUNTS_FILE.exists():
        print("测试账号已存在，保留原密码及独立测试账号文件。")
        return 0
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(text, encoding="utf-8")
    os.chmod(ACCOUNTS_FILE, 0o600)
    # Never print passwords.
    print(f"已写入测试账号文件：{ACCOUNTS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
