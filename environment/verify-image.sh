#!/usr/bin/env bash
set -euo pipefail

for url in \
  http://127.0.0.1:8081/health \
  http://127.0.0.1:8082/health \
  http://127.0.0.1:8083/health \
  http://127.0.0.1:9222/json/version
do
  curl -fsS "$url" >/dev/null
done

test -x /opt/services/engine/sim_engine
test -s /opt/services/ground_truth.json
test -s /var/lib/cluster-ops/cluster.db

if su -s /bin/bash agent -c "test -r /opt/services/engine/sim_engine ||
  test -r /opt/services/ground_truth.json ||
  test -r /var/lib/cluster-ops/cluster.db ||
  test -r /etc/supervisor/conf.d/cluster.conf"; then
  echo "SECURITY_ERROR: agent can read protected backend files" >&2
  exit 1
fi

su -s /bin/bash agent -c 'python3 -c "import os; from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    assert os.access(p.chromium.executable_path, os.X_OK)"'

echo "IMAGE_VERIFY_OK"
