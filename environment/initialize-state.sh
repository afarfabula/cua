#!/usr/bin/env bash
set -euo pipefail

gitea_dir=/var/lib/gitea
gitea_config=/etc/gitea/app.ini
staging=$(mktemp -d)
gitea_pid=

stop_gitea() {
    if [ -n "$gitea_pid" ]; then
        kill "$gitea_pid" 2>/dev/null || true
        wait "$gitea_pid" 2>/dev/null || true
        gitea_pid=
    fi
}

cleanup() {
    local status=$?
    stop_gitea
    if [ "$status" -ne 0 ] && [ -r "$staging/gitea.log" ]; then
        cat "$staging/gitea.log" >&2
    fi
    rm -rf -- "$staging"
}
trap cleanup EXIT

chown -R gitea:gitea "$gitea_dir" "$(dirname "$gitea_config")"
chmod 700 "$gitea_dir" "$(dirname "$gitea_config")"
chown gitea:gitea "$staging"

python3 -c "import sys; sys.path.insert(0, '/opt/services/portal'); import app; app.init_db(reset=True)"
runuser -u gitea -- env GITEA_WORK_DIR="$gitea_dir" /opt/bin/gitea web \
    --config "$gitea_config" >"$staging/gitea.log" 2>&1 &
gitea_pid=$!

runuser -u gitea -- python3 /opt/services/gitea_seed.py \
    --gitea-bin /opt/bin/gitea \
    --gitea-config "$gitea_config" \
    --gitea-work "$gitea_dir" \
    --baseline-out "$staging/gitea_baseline.json"

stop_gitea
install -m 600 "$staging/gitea_baseline.json" /opt/services/gitea_baseline.json
mkdir -p /opt/services/clean_state/{workspace,gitea,portal}
cp -a /workspace/. /opt/services/clean_state/workspace/
cp -a "$gitea_dir/." /opt/services/clean_state/gitea/
cp -a /var/lib/clearing-portal/. /opt/services/clean_state/portal/
