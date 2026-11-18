#!/usr/bin/env bash
# 查看本项目应用状态。不打印密码或授权码。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_PID_FILE="$ROOT/data/server.pid"
PORT="${APP_PORT:-5050}"

echo "项目目录：$ROOT"

if [[ -f "$APP_PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$APP_PID_FILE" || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "应用：运行中，PID $pid"
  else
    echo "应用：PID 文件存在但进程不在（$pid）"
  fi
else
  echo "应用：未运行"
fi

code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}/health" || true)"
echo "本机 /health：$code"
login_code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}/login" || true)"
echo "本机 /login：$login_code"
