#!/usr/bin/env python3
"""SMTP 登录连通性检查：只做连接、登录和 QUIT，不发送任何邮件。

同一步连续失败两次即停止。授权码只从环境变量读取，不会被打印或写入文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.mailer import load_mail_config, check_smtp_login  # noqa: E402


def main() -> int:
    cfg = load_mail_config()
    print("SMTP 登录检查（不发送邮件）")
    print(f"  主机: {cfg.host or '（未配置）'}")
    print(f"  端口: {cfg.port}")
    print(f"  用户名: {cfg.username or '（未配置）'}")
    print(f"  发件人: {cfg.sender or '（未配置）'}")
    print(f"  SSL: {cfg.use_ssl}  TLS: {cfg.use_tls}")
    print(f"  密码来源环境变量: {cfg.password_env}")
    print(f"  密码已设置: {'是' if cfg.password_configured else '否'}")
    print(f"  代理: {cfg.proxy_host}:{cfg.proxy_port}")
    print(f"  真实发送开关 MAIL_SEND_ENABLED: {cfg.send_enabled}")
    report = check_smtp_login(max_attempts=2)
    for step in report.get("steps") or []:
        flag = "成功" if step.get("ok") else "失败"
        extra = ""
        if not step.get("ok") and step.get("error"):
            extra = f" — {step['error']}"
        print(f"  步骤 {step['step']} 第 {step['attempt']} 次：{flag}{extra}")
    if report.get("ok"):
        print("结果：SMTP 登录检查通过（已 QUIT，未发送邮件）。")
        return 0
    print("结果：SMTP 登录检查未通过，已停止。")
    if report.get("failed_step"):
        print(f"  失败步骤: {report['failed_step']}")
    if report.get("error"):
        print(f"  原因: {report['error']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
