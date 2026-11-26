#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/log/supervisor /var/log/grafana /var/lib/mailpit /var/lib/clearing-portal /workspace/output
chown -R agent:agent /workspace

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

if [ ! -f /var/run/clearing_services_booted ]; then
  /usr/bin/supervisord -c /etc/supervisor/conf.d/clearing.conf
  # Wait for Mailpit SMTP & seed emails once
  wait_for_http http://127.0.0.1:8025/api/v1/messages
  if [ ! -f /var/lib/mailpit/.seeded ]; then
    python3 /opt/services/mailpit_seed.py --seed-dir /opt/services/seed --smtp-host 127.0.0.1 --smtp-port 1025
    touch /var/lib/mailpit/.seeded
  fi
  # Ensure portal, gitea, grafana, prometheus, perfetto are up
  for url in \
    http://127.0.0.1:8080/health \
    http://127.0.0.1:3001/api/v1/version \
    http://127.0.0.1:9090/-/ready \
    http://127.0.0.1:3000/api/health \
    http://127.0.0.1:9001/
  do
    wait_for_http "$url"
  done
  touch /var/run/clearing_services_booted
fi

exec "$@"
