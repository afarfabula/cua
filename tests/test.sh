#!/usr/bin/env bash
set -euo pipefail

# 删除 rollout 中的截图，避免产物过大导致下载失败
find /logs/agent/browser -type f \( -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' \) -delete 2>/dev/null || true

exec python3 /tests/grade.py
