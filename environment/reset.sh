#!/usr/bin/env bash
set -euo pipefail

supervisorctl -c /etc/supervisor/conf.d/cluster.conf stop rack-console team-desk finops-cab

rm -rf /workspace/*
cp -a /opt/services/clean_state/workspace/. /workspace/
mkdir -p /workspace/output
chown -R agent:agent /workspace

rm -rf /var/lib/cluster-ops/*
cp -a /opt/services/clean_state/db/. /var/lib/cluster-ops/
chmod 700 /var/lib/cluster-ops
chmod 600 /var/lib/cluster-ops/*

supervisorctl -c /etc/supervisor/conf.d/cluster.conf start rack-console team-desk finops-cab
for url in \
  http://127.0.0.1:8081/health \
  http://127.0.0.1:8082/health \
  http://127.0.0.1:8083/health
do
  for _ in $(seq 1 30); do
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && break
    sleep 0.5
  done
done
echo "RESET_COMPLETE"
