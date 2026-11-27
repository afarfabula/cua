#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/log/supervisor /var/lib/cluster-ops /workspace/output
chown -R agent:agent /workspace
chmod 700 /opt/services /var/lib/cluster-ops

wait_for_http() {
  local url=$1
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  printf 'Service did not become ready: %s\n' "$url" >&2
  return 1
}

if [ ! -f /var/run/cluster_services_booted ]; then
  /usr/bin/supervisord -c /etc/supervisor/conf.d/cluster.conf
  for url in \
    http://127.0.0.1:8081/health \
    http://127.0.0.1:8082/health \
    http://127.0.0.1:8083/health \
    http://127.0.0.1:9222/json/version
  do
    wait_for_http "$url"
  done
  touch /var/run/cluster_services_booted
fi

exec "$@"
