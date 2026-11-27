#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/log/supervisor /var/log/grafana /var/lib/mailpit /var/lib/clearing-portal /workspace/output /opt/services/traces
chown -R agent:agent /workspace
chmod 700 /opt/services/traces

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

# Block agent terminal from directly accessing service ports.
# The root Chromium browser (CDP :9222) is the only path for Portal/Grafana/Perfetto.
# If iptables is unavailable (no CAP_NET_ADMIN), Portal also enforces UID checks as fallback.
setup_iptables() {
  local blocked=0
  for port in 8080 3000 9090 9001 13000 19090 19001; do
    if iptables -A OUTPUT -o lo -p tcp --dport "$port" -m owner --uid-owner agent -j REJECT 2>/dev/null; then
      blocked=$((blocked + 1))
    fi
  done
  if [ "$blocked" -eq 7 ]; then
    echo "[entrypoint] iptables: blocked agent from ports 8080/3000/9090/9001/13000/19090/19001"
  else
    echo "[entrypoint] WARNING: iptables unavailable (blocked $blocked/7), relying on Portal/proxy UID checks"
  fi
}

if [ ! -f /var/run/clearing_services_booted ]; then
  /usr/bin/supervisord -c /etc/supervisor/conf.d/clearing.conf
  setup_iptables
  # Wait for Mailpit SMTP & seed emails once
  wait_for_http http://127.0.0.1:8025/api/v1/messages
  if [ ! -f /var/lib/mailpit/.seeded ]; then
    python3 /opt/services/mailpit_seed.py --seed-dir /opt/services/seed --smtp-host 127.0.0.1 --smtp-port 1025
    touch /var/lib/mailpit/.seeded
  fi
  # Critical services must be ready (Portal, Gitea, CDP)
  for url in \
    http://127.0.0.1:8080/health \
    http://127.0.0.1:3001/api/v1/version \
    http://127.0.0.1:9222/json/version
  do
    wait_for_http "$url"
  done
  # Non-critical services: best-effort (Grafana/Prometheus may be slow; Perfetto uses Unix socket)
  for url in \
    http://127.0.0.1:19090/-/ready \
    http://127.0.0.1:13000/api/health
  do
    wait_for_http "$url" || true
  done
  # Check Perfetto Unix socket exists
  for _ in $(seq 1 30); do
    [ -S /run/perfetto.sock ] && break
    sleep 0.5
  done
  touch /var/run/clearing_services_booted
fi

exec "$@"
