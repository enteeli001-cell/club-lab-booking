#!/usr/bin/env bash
# 停止本项目应用。
set -euo pipefail
APP_ONLY=0
if [[ "${1:-}" == "--app-only" ]]; then APP_ONLY=1; elif [[ $# -gt 0 ]]; then echo "用法：$0 [--app-only]" >&2; exit 2; fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/data/server.pid"

if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" || true
    for i in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.4
    done
    if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" || true; fi
    echo "已停止应用进程 $pid。"
  else
    echo "应用进程已不存在。"
  fi
  rm -f "$PID_FILE"
else
  echo "没有正在运行的应用（未找到 $PID_FILE）。"
fi
