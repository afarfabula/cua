#!/usr/bin/env bash
set -euo pipefail

# Reset workspace (including dynamic canary trace), Mailpit, Gitea, and Portal back to clean frozen snapshot
supervisorctl -c /etc/supervisor/conf.d/clearing.conf stop mailpit gitea portal

rm -rf /workspace/*
cp -a /opt/services/clean_state/workspace/. /workspace/
mkdir -p /workspace/output
chown -R agent:agent /workspace

rm -rf /var/lib/gitea/*
cp -a /opt/services/clean_state/gitea/. /var/lib/gitea/
chown -R gitea:gitea /var/lib/gitea
chmod 700 /var/lib/gitea

rm -rf /var/lib/clearing-portal/*
cp -a /opt/services/clean_state/portal/. /var/lib/clearing-portal/
chmod -R 700 /var/lib/clearing-portal

rm -rf /var/lib/mailpit/*

supervisorctl -c /etc/supervisor/conf.d/clearing.conf start mailpit gitea portal
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8025/api/v1/messages >/dev/null 2>&1 \
     && curl -fsS http://127.0.0.1:3001/api/v1/version >/dev/null 2>&1 \
     && curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
python3 /opt/services/mailpit_seed.py --seed-dir /opt/services/seed --smtp-host 127.0.0.1 --smtp-port 1025
touch /var/lib/mailpit/.seeded
echo "RESET_COMPLETE"
