import os
import socket
import sys

# Line-buffer stdout/stderr when daemonized.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

from app import create_app
from app.paths import API_TOKEN_FILE, ADMIN_PASSWORD_FILE, PID_FILE, ROOT_DIR, ensure_runtime_dirs

app = create_app()


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def choose_port(host: str, preferred: int, span: int = 40) -> int:
    for port in range(preferred, preferred + span):
        if port_is_free(host, port):
            return port
    raise RuntimeError(f"在 {host}:{preferred}-{preferred + span - 1} 范围内没有可用端口")


def write_pid(pid_path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid) + "\n", encoding="utf-8")


if __name__ == "__main__":
    ensure_runtime_dirs()
    host = os.environ.get("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    preferred = int(os.environ.get("APP_PORT", "5050"))
    strict = os.environ.get("APP_PORT_STRICT", "").strip() in {"1", "true", "yes", "on"}
    if strict:
        if not port_is_free(host, preferred):
            print(f"端口 {host}:{preferred} 已被占用。", file=sys.stderr)
            sys.exit(1)
        port = preferred
    else:
        try:
            port = choose_port(host, preferred)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)
        if port != preferred:
            print(f"端口 {preferred} 已被占用，改用 {port}")

    from app.jobs import start_background_scanner

    start_background_scanner(app)
    write_pid(PID_FILE, os.getpid())
    print(f"项目目录：{ROOT_DIR}")
    print(f"监听地址：http://{host}:{port}")
    print(f"初始管理员密码文件：{ADMIN_PASSWORD_FILE}")
    print(f"API 令牌文件：{API_TOKEN_FILE}")
    print(f"PID 文件：{PID_FILE}")
    use_dev = os.environ.get("USE_FLASK_DEV", "").strip() in {"1", "true", "yes", "on"}
    try:
        if use_dev:
            print("使用 Flask 开发服务器（USE_FLASK_DEV=1）。")
            app.run(host=host, port=port, debug=False, use_reloader=False)
        else:
            from waitress import serve

            print("使用 waitress 提供服务。")
            serve(
                app,
                host=host,
                port=port,
                threads=int(os.environ.get("APP_THREADS", "8")),
                ident="club-lab-system",
                channel_timeout=120,
            )
    finally:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
