#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cache=${1:-"$here/binaries"}
destination=${2:-"$cache"}
proxy=${GITHUB_PROXY-https://gh-proxy.com}
mkdir -p "$destination"

verify() {
    printf '%s  %s\n' "$1" "$2" | sha256sum --status -c -
}

download() {
    local url=$1 path=$2
    local base_flags=(--fail --silent --show-error --location --connect-timeout 15
                      --max-time 180 --retry 2 --retry-all-errors --retry-max-time 240)
    curl "${base_flags[@]}" --output "$path" "$url"
}

fetch() {
    local name=$1 url=$2 digest=$3 cached="$cache/$1" out="$destination/$1" tmp
    if [ -s "$cached" ] && verify "$digest" "$cached"; then
        if [ "$cached" != "$out" ]; then
            cp -- "$cached" "$out"
        fi
        return
    fi
    if [ -s "$cached" ]; then
        printf 'Ignoring cached %s: SHA-256 mismatch\n' "$name" >&2
    fi

    tmp=$(mktemp "$destination/.${name}.XXXXXX")
    if [ -n "$proxy" ] && download "${proxy%/}/$url" "$tmp" && verify "$digest" "$tmp"; then
        mv -- "$tmp" "$out"
        return
    fi
    if download "$url" "$tmp" && verify "$digest" "$tmp"; then
        mv -- "$tmp" "$out"
        return
    fi
    rm -f -- "$tmp"
    printf 'Could not download and verify %s\n' "$name" >&2
    return 1
}

# SHA-256 digests are pinned to the upstream GitHub release assets.
fetch gitea-28.4.0-linux-amd64.xz \
    https://github.com/go-gitea/gitea/releases/download/v28.4.0/gitea-28.4.0-linux-amd64.xz \
    0b60b8efd1c7d948d927ef29e849d7263eb000c3cb2ae5d4303e3ecbdeaaa237
fetch mailpit-linux-amd64.tar.gz \
    https://github.com/axllent/mailpit/releases/download/v1.34.0/mailpit-linux-amd64.tar.gz \
    af236b42f171b1c74f0ac5ff444f44d36f2f2ecc0134f5a2f1d4e307e56a0d2c
fetch prometheus-3.16.0.linux-amd64.tar.gz \
    https://github.com/prometheus/prometheus/releases/download/v3.16.0/prometheus-3.16.0.linux-amd64.tar.gz \
    68727009472a2c02e1ff7f062fd5b3e637285bffc8fc2d340c76fb449d6e0f34
fetch perfetto-ui.zip \
    https://github.com/google/perfetto/releases/download/v59.1/perfetto-ui.zip \
    df4433f5d8d98d2c490249870944c819ff85206f61e55e5983bbcc2338c83904
fetch perfetto-linux-amd64.zip \
    https://github.com/google/perfetto/releases/download/v59.1/linux-amd64.zip \
    7deb8c2572c6ae7ec80cd719d388041079827b0c44a1659694d5d88faa4f501e
fetch grafana_13.3.2_linux_amd64.deb \
    https://github.com/grafana/grafana/releases/download/v13.3.2/grafana_13.3.2_44555861735_linux_amd64.deb \
    ab62d8122404cc7510778d4f1c8cd7e8414b95f781a7a3d13f89f07ebb9382a6
