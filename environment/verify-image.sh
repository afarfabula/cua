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
test -s /workspace/traces/clearing_incident.pftrace
test -s /workspace/contracts/vip_merchant_framework_2026.pdf
test -s /opt/services/seed/sla_corridor_matrix.png
test -s /opt/services/seed/dba_pg_lock_graph.png
test -s /opt/services/seed/compliance_errata_20261124.pdf

# Ensure agent user cannot access protected ground truth or portal SQLite DB
if su -s /bin/bash agent -c "test -r /opt/services/ground_truth.json || test -r /var/lib/clearing-portal/portal.db"; then
  echo "SECURITY_ERROR: agent can read protected verifier files" >&2
  exit 1
fi

echo "IMAGE_VERIFY_OK"
