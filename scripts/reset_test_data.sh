#!/usr/bin/env bash
# 仅清理独立测试目录 data/testing 和 logs/testing；生产库、账号、隧道均不操作。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/reset_test_data.py"
