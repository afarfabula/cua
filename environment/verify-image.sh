#!/usr/bin/env bash
set -euo pipefail

# Verify all 6 services + Perfetto CLI + v2 multimodal artifacts and permission isolation
for url in \
  http://127.0.0.1:3000/api/health \
  http://127.0.0.1:9001/ \
  http://127.0.0.1:3001/api/v1/version \
  http://127.0.0.1:8025/api/v1/messages \
  http://127.0.0.1:8080/health \
  http://127.0.0.1:9090/-/ready
do
  curl -fsS "$url" >/dev/null
done

test -x /usr/local/bin/trace_processor_shell
test -s /opt/services/traces/clearing_incident.pftrace
# agent must NOT be able to read trace files directly
if su -s /bin/bash agent -c "test -r /opt/services/traces/clearing_incident.pftrace"; then
  echo "SECURITY_ERROR: agent can read trace file directly" >&2
  exit 1
fi
test -s /workspace/contracts/vip_merchant_framework_2026.pdf
test -s /opt/services/seed/sla_corridor_matrix.png
test -s /opt/services/seed/dba_pg_lock_graph.png
test -s /opt/services/seed/compliance_errata_20261124.pdf

# Confirm the service can read its files while the task user cannot read backend databases.
su -s /bin/bash gitea -c "test -r /var/lib/gitea/gitea.db && test -r /etc/gitea/app.ini"
if su -s /bin/bash agent -c "test -r /opt/services/ground_truth.json ||
  test -r /var/lib/clearing-portal/portal.db || test -r /var/lib/gitea/gitea.db ||
  test -r /etc/gitea/app.ini"; then
  echo "SECURITY_ERROR: agent can read protected backend files" >&2
  exit 1
fi

su -s /bin/bash agent -c 'python3 -c "import os; from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    assert os.access(p.chromium.executable_path, os.X_OK)"'

echo "IMAGE_VERIFY_OK"
