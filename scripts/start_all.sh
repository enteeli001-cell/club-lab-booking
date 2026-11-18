#!/usr/bin/env bash
# 启动本项目应用（waitress）。--app-only 参数保留以兼容旧用法。
# 不修改系统代理/DNS/防火墙；不打印密码或授权码。
set -euo pipefail
APP_ONLY=0
if [[ "${1:-}" == "--app-only" ]]; then APP_ONLY=1; elif [[ $# -gt 0 ]]; then echo "用法：$0 [--app-only]" >&2; exit 2; fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-5050}"
PID_FILE="$ROOT/data/server.pid"
LOG_FILE="$ROOT/logs/server.out"
APP_LOG="$ROOT/logs/app.log"

mkdir -p "$ROOT/data" "$ROOT/logs"

rotate_if_large() {
  local f="$1"
  if [[ -f "$f" ]]; then
    local sz
    sz="$(wc -c < "$f" | tr -d ' ')"
    if [[ "$sz" -gt 5000000 ]]; then
      mv -f "$f" "$f.1"
    fi
  fi
}

if [[ -f "$PID_FILE" ]]; then
  old="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "应用已在运行，PID $old。请先运行 scripts/stop_all.sh 或 scripts/status.sh。"
  else
    rm -f "$PID_FILE"
  fi
fi

if [[ ! -f "$PID_FILE" ]] || ! kill -0 "$(tr -d '[:space:]' < "$PID_FILE")" 2>/dev/null; then
  rotate_if_large "$LOG_FILE"
  rotate_if_large "$APP_LOG"
  echo "启动应用（waitress）监听 ${HOST}:${PORT} …"
  PYTHONUNBUFFERED=1 APP_HOST="$HOST" APP_PORT="$PORT" APP_PORT_STRICT=1 \
    nohup "$ROOT/.venv/bin/python" "$ROOT/run.py" >> "$LOG_FILE" 2>&1 &
  # run.py 会写入 PID；等待监听就绪
  for i in $(seq 1 30); do
    if [[ -f "$PID_FILE" ]] && kill -0 "$(tr -d '[:space:]' < "$PID_FILE")" 2>/dev/null; then
      if "$ROOT/.venv/bin/python" -c "import socket,sys; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1', int(sys.argv[1]))); s.close(); raise SystemExit(0 if r==0 else 1)" "$PORT"; then
        echo "应用已启动，PID $(tr -d '[:space:]' < "$PID_FILE")，日志 $LOG_FILE"
        break
      fi
    fi
    if [[ "$i" -eq 30 ]]; then
      echo "应用未能在时限内开始监听 ${PORT}。" >&2
      exit 1
    fi
    sleep 0.3
  done
fi
