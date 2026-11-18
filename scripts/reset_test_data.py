#!/usr/bin/env python3
"""Reset isolated demo data only. Production data/app.db and production secrets stay intact."""
import shutil
from demo_config import ROOT, DEMO_DATA, DEMO_LOGS, create_demo_app


def main():
    targets = [DEMO_DATA, DEMO_LOGS]
    # Validate all paths before deleting anything; never follow a symlink into production.
    for target in targets:
        if target.resolve() != target or target.is_symlink():
            raise SystemExit('拒绝清理：测试目录或上层路径含符号链接。')
        if target not in (ROOT/'data'/'testing', ROOT/'logs'/'testing'):
            raise SystemExit('拒绝清理：目标不是独立测试目录。')
    for target in targets:
        if target.exists():shutil.rmtree(target)
    app = create_demo_app()
    with app.app_context():
        from app import db
        db.session.remove();db.engine.dispose()
    print('已重建独立测试库 data/testing/app.db；生产数据、账号密码及隧道未操作。')
    print('载入示例数据：.venv/bin/python scripts/seed_test_data.py')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
