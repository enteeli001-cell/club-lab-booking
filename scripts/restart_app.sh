#!/usr/bin/env bash
# 重启应用；启动能力检查失败时保持现有进程和 PID 文件不变。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if ! "$ROOT/.venv/bin/python" -c 'import socket; s=socket.socket(); s.close()'; then
  echo "当前环境不允许创建监听套接字，未停止应用。" >&2
  exit 1
fi
bash "$ROOT/scripts/stop_all.sh"
bash "$ROOT/scripts/start_all.sh"
echo "应用已重启。"
