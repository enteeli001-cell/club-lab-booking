#!/usr/bin/env python3
"""Set staff/test passwords from environment variables. Never print or log them.

SUPER_ADMIN_NEW_PASSWORD
  Sets the superadmin password (non-empty; unique among administrators;
  strength rules skipped; must_change_password left false).

--also-captain STUDENT_ID
  Also sets that captain's password to SUPER_ADMIN_NEW_PASSWORD, skipping
  captain strength rules and not requiring a first-login change. Sharing
  a password with an administrator is allowed (different login entrances).

--test-admin USERNAME_OR_ID
  Sets that test administrator's password from TEST_ADMIN_NEW_PASSWORD
  (strength skipped, no first-login change). Still must differ from every
  other administrator password; duplicates cause a non-zero exit.
  On success, User.set_password also stores a Fernet ciphertext on the
  admin record (key file data/pw_view_key) so a superadmin can later view
  the password in 管理员管理. The script also updates data/test_accounts.txt
  (mode 600). The value is never printed or logged.
  The captain line in that file stays "密码：同总管理员密码".
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not str(value):
        print(f"未设置环境变量 {name}。", file=sys.stderr)
        return None
    return value


def _find_test_admin(User, token: str):
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        user = User.query.get(int(token))
        if user is not None and user.is_admin:
            return user
    user = User.query.filter_by(student_id=token).first()
    if user is not None and user.is_admin:
        return user
    user = User.query.filter_by(name=token, role="admin").first()
    return user



ACCOUNTS_FILE = ROOT / "data" / "test_accounts.txt"


def _sync_test_admin_ciphertext(user, password: str) -> Path:
    """Write the test-admin password into the gitignored accounts file. Never print it."""
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    student_id = user.student_id or ""
    name = user.name or ""
    user_id = user.id
    text = (
        "测试账号（请妥善保管，勿对外公开）\n"
        "\n"
        "测试队长\n"
        "  学号：TEST001\n"
        "  姓名：测试队长\n"
        "  队伍：测试队\n"
        "  登录入口：普通登录（学号+密码）\n"
        "  密码：同总管理员密码\n"
        "\n"
        "测试管理员\n"
        f"  学号：{student_id}\n"
        f"  姓名：{name}\n"
        f"  ID：{user_id}\n"
        "  登录入口：管理员登录（只输入密码）\n"
        f"  密码：{password}\n"
        "\n"
        "也可用脚本设置/覆盖密码（不要把密码写进命令或脚本文件）：\n"
        "  SUPER_ADMIN_NEW_PASSWORD=… TEST_ADMIN_NEW_PASSWORD=… \\\n"
        "    .venv/bin/python scripts/set_super_admin_password.py \\\n"
        "    --also-captain TEST001 --test-admin testadmin\n"
    )
    # If the file already exists, keep the captain section and usage, only replace
    # or append the test-admin password block so other notes are not required.
    if ACCOUNTS_FILE.exists():
        existing = ACCOUNTS_FILE.read_text(encoding="utf-8")
        # Replace a previous 测试管理员 block's password line when present;
        # otherwise write the canonical file (captain password stays a pointer).
        import re
        pattern = re.compile(
            r"(测试管理员\n(?:  [^\n]*\n)*?  密码：)[^\n]*",
            re.M,
        )
        if pattern.search(existing):
            # Also refresh 学号/姓名/ID lines if present
            def _repl(_m, pw=password):
                return _m.group(1) + pw
            text = pattern.sub(_repl, existing, count=1)
            # Ensure ID / 学号 / 姓名 stay accurate without exposing other secrets
            text = re.sub(r"(测试管理员\n(?:  [^\n]*\n)*?  学号：)[^\n]*", r"\g<1>" + student_id, text, count=1)
            text = re.sub(r"(测试管理员\n(?:  [^\n]*\n)*?  姓名：)[^\n]*", r"\g<1>" + name, text, count=1)
            if re.search(r"测试管理员\n(?:  [^\n]*\n)*?  ID：", text):
                text = re.sub(r"(测试管理员\n(?:  [^\n]*\n)*?  ID：)[^\n]*", r"\g<1>" + str(user_id), text, count=1)
            elif "测试管理员" in text:
                text = text.replace("测试管理员\n", f"测试管理员\n  ID：{user_id}\n", 1)
    ACCOUNTS_FILE.write_text(text, encoding="utf-8")
    os.chmod(ACCOUNTS_FILE, 0o600)
    return ACCOUNTS_FILE


def main() -> int:
    parser = argparse.ArgumentParser(description="设置总管理员/测试账号密码（不打印密码）。")
    parser.add_argument(
        "--also-captain",
        metavar="STUDENT_ID",
        help="同时将该学号的测试队长密码设为与总管理员相同的值",
    )
    parser.add_argument(
        "--test-admin",
        metavar="USERNAME_OR_ID",
        help="按学号、姓名或数字 ID 指定测试管理员，并用 TEST_ADMIN_NEW_PASSWORD 设置其密码",
    )
    args = parser.parse_args()

    update_super = bool(args.also_captain) or not args.test_admin or os.environ.get("SUPER_ADMIN_NEW_PASSWORD")
    # If the user only wants to set the test admin, SUPER_ADMIN_NEW_PASSWORD is optional.
    if args.test_admin and not args.also_captain and not os.environ.get("SUPER_ADMIN_NEW_PASSWORD"):
        update_super = False

    super_password = None
    if update_super or args.also_captain:
        super_password = _require_env("SUPER_ADMIN_NEW_PASSWORD")
        if super_password is None:
            return 1

    test_password = None
    if args.test_admin:
        test_password = _require_env("TEST_ADMIN_NEW_PASSWORD")
        if test_password is None:
            return 1

    if super_password is None and test_password is None:
        print("请设置 SUPER_ADMIN_NEW_PASSWORD，或配合 --test-admin 设置 TEST_ADMIN_NEW_PASSWORD。", file=sys.stderr)
        return 1

    from app import create_app, db
    from app.models import User
    from app.utils import staff_password_is_unique

    app = create_app()
    with app.app_context():
        admin = User.query.filter_by(role="superadmin").order_by(User.id.asc()).first()
        if admin is None:
            admin = User.query.filter_by(student_id="admin").first()

        captain = None
        if args.also_captain:
            sid = args.also_captain.strip()
            captain = User.query.filter_by(student_id=sid).first()
            if captain is None:
                print("未找到指定的测试队长账号。", file=sys.stderr)
                return 1
            if captain.is_staff:
                print("指定账号不是队长。", file=sys.stderr)
                return 1
            if super_password is None:
                print("设置测试队长密码需要环境变量 SUPER_ADMIN_NEW_PASSWORD。", file=sys.stderr)
                return 1

        test_admin = None
        if args.test_admin:
            test_admin = _find_test_admin(User, args.test_admin)
            if test_admin is None:
                print("未找到指定的测试管理员账号。", file=sys.stderr)
                return 1
            if test_admin.is_superadmin:
                print("不能用 --test-admin 修改总管理员。", file=sys.stderr)
                return 1

        if super_password is not None:
            if admin is None:
                print("未找到总管理员账号。", file=sys.stderr)
                return 1
            if not staff_password_is_unique(super_password, exclude_id=admin.id):
                # If we will also set the test admin to a different password, still
                # cannot clash with *other* existing admins (including current test admin).
                print("新密码与其他管理员密码重复。", file=sys.stderr)
                return 1
            if test_password is not None and super_password == test_password:
                print("总管理员密码与测试管理员密码不能相同。", file=sys.stderr)
                return 1

        if test_password is not None:
            if not staff_password_is_unique(test_password, exclude_id=test_admin.id):
                print("测试管理员密码与其他管理员密码重复。", file=sys.stderr)
                return 1

        notes = []
        if super_password is not None:
            admin.role = "superadmin"
            admin.set_password(super_password, require_change=False)
            notes.append("已更新总管理员密码")
            if captain is not None:
                captain.set_password(super_password, require_change=False)
                notes.append("已同步测试队长密码")
        if test_password is not None:
            test_admin.set_password(test_password, require_change=False)
            notes.append("已更新测试管理员密码")
        db.session.commit()

        if test_password is not None:
            path = _sync_test_admin_ciphertext(test_admin, test_password)
            notes.append(f"已同步写入测试账号文件：{path}")

    print("，".join(notes) + "。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
