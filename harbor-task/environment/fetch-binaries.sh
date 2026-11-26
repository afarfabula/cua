#!/bin/bash
# Pre-download large github release binaries into environment/binaries/ so the
# Docker build COPYs them instead of curling through a (slow/unreliable) proxy.
# Run this once before `docker build` if github.com is slow or unreachable.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)/binaries"
mkdir -p "$DIR"
cd "$DIR"

PROXY="${GITHUB_PROXY:-https://gh-proxy.com}"

fetch() {
    local name="$1" url="$2"
    if [ -s "$name" ]; then
        echo "  [skip] $name already exists ($(du -h "$name" | cut -f1))"
        return
    fi
    echo "  [get]  $name <- $url"
    curl -fSL --retry 5 --retry-all-errors "$url" -o "$name"
}

echo "Fetching binaries into $DIR"

fetch gitea-28.4.0-linux-amd64.xz \
    "https://dl.gitea.com/gitea/28.4.0/gitea-28.4.0-linux-amd64.xz"

fetch mailpit-linux-amd64.tar.gz \
    "${PROXY}/https://github.com/axllent/mailpit/releases/download/v1.34.0/mailpit-linux-amd64.tar.gz"

fetch prometheus-3.16.0.linux-amd64.tar.gz \
    "${PROXY}/https://github.com/prometheus/prometheus/releases/download/v3.16.0/prometheus-3.16.0.linux-amd64.tar.gz"

fetch perfetto-ui.zip \
    "${PROXY}/https://github.com/google/perfetto/releases/download/v59.1/perfetto-ui.zip"

fetch perfetto-linux-amd64.zip \
    "${PROXY}/https://github.com/google/perfetto/releases/download/v59.1/linux-amd64.zip"

fetch grafana_13.3.2_linux_amd64.deb \
    "${PROXY}/https://github.com/grafana/grafana/releases/download/v13.3.2/grafana_13.3.2_44555861735_linux_amd64.deb"

echo "Done. Sizes:"
ls -lh "$DIR"
