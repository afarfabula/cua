FROM python:3.11.11-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /workspace

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|https\?://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    iptables \
    sqlite3 \
    supervisor \
    unzip \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Install pinned Python packages & Playwright Chromium (trimmed docs/cache to stay well under 2GB)
RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    pydantic==2.10.4 \
    requests==2.32.3 \
    pillow==11.1.0 \
    reportlab==4.2.5 \
    pytest==8.3.4 \
    python-multipart==0.0.20 \
    playwright==1.50.0 \
    && playwright install chromium --with-deps \
    && chmod -R a+rX /opt/ms-playwright \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Create non-root agent user
RUN useradd -m -s /bin/bash agent \
    && useradd --system --user-group --home-dir /var/lib/gitea --shell /usr/sbin/nologin gitea

# Install pinned lightweight open-source binaries (Gitea, Mailpit, Prometheus, Perfetto UI + trace_processor_shell, Grafana)
# Keep optional cached archives in this build stage. The runtime image receives only installed files.
FROM base AS release-assets
ARG GITHUB_PROXY=https://gh-proxy.com
COPY binaries/ /tmp/binaries/
COPY fetch-binaries.sh /opt/services/fetch-binaries.sh
RUN mkdir -p /opt/bin /opt/perfetto-ui /opt/grafana \
    && bash /opt/services/fetch-binaries.sh /tmp/binaries /tmp/releases \
    && xz -dc /tmp/releases/gitea-28.4.0-linux-amd64.xz > /opt/bin/gitea \
    && chmod 755 /opt/bin/gitea \
    && tar -xzf /tmp/releases/mailpit-linux-amd64.tar.gz -C /opt/bin mailpit \
    && chmod 755 /opt/bin/mailpit \
    && tar -xzf /tmp/releases/prometheus-3.16.0.linux-amd64.tar.gz -C /opt/bin --strip-components=1 \
        prometheus-3.16.0.linux-amd64/prometheus prometheus-3.16.0.linux-amd64/promtool \
    && python3 -m zipfile -e /tmp/releases/perfetto-ui.zip /opt/perfetto-ui \
    && python3 -m zipfile -e /tmp/releases/perfetto-linux-amd64.zip /tmp/perfetto-bins \
    && install -m 755 /tmp/perfetto-bins/linux-amd64/trace_processor_shell /usr/local/bin/trace_processor_shell \
    && dpkg-deb -x /tmp/releases/grafana_13.3.2_linux_amd64.deb /opt/grafana \
    && rm -rf /tmp/releases /tmp/perfetto-bins

FROM base
COPY --from=release-assets /opt/bin/ /opt/bin/
COPY --from=release-assets /opt/perfetto-ui/ /opt/perfetto-ui/
COPY --from=release-assets /opt/grafana/ /opt/grafana/
COPY --from=release-assets /usr/local/bin/trace_processor_shell /usr/local/bin/trace_processor_shell
RUN mkdir -p /var/lib/gitea /var/lib/mailpit /var/lib/prometheus/data /var/lib/grafana/data \
    /var/lib/clearing-portal /var/log/supervisor /var/log/grafana /opt/services/traces

# Copy initial workspace files
COPY workspace_init/ /workspace/

# Copy service definitions, configs, and seed scripts
COPY services/portal/app.py /opt/services/portal/app.py
COPY seed/pftrace_encoder.py /opt/services/portal/pftrace_encoder.py
COPY services/gitea/app.ini /etc/gitea/app.ini
COPY services/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY services/grafana/grafana.ini /etc/grafana/grafana.ini
COPY services/grafana/provisioning/ /etc/grafana/provisioning/
COPY services/perfetto/perfetto_server.py /opt/services/perfetto_server.py
COPY services/uid_proxy.py /opt/services/uid_proxy.py
COPY seed/pftrace_encoder.py /opt/services/pftrace_encoder.py
COPY seed/generate_data.py /opt/services/generate_data.py
COPY seed/gitea_seed.py /opt/services/gitea_seed.py
COPY seed/mailpit_seed.py /opt/services/mailpit_seed.py
COPY supervisord.conf /etc/supervisor/conf.d/clearing.conf
COPY entrypoint.sh /opt/services/entrypoint.sh
COPY reset.sh /opt/services/reset.sh
COPY initialize-state.sh /opt/services/initialize-state.sh
RUN chmod +x /opt/services/entrypoint.sh /opt/services/reset.sh

# Generate frozen logs, binary .pftrace, OpenMetrics TSDB blocks, visual PNG/PDFs, contracts, and verifier truth
# Trace files go to /opt/services/traces (root-only) so agent terminal & trace_processor_shell cannot read them
RUN python3 /opt/services/generate_data.py \
      --workspace-dir /workspace \
      --seed-dir /opt/services/seed \
      --truth-path /opt/services/ground_truth.json \
      --trace-dir /opt/services/traces \
    && /opt/bin/promtool tsdb create-blocks-from openmetrics /opt/services/seed/metrics.openmetrics /var/lib/prometheus/data \
    && chmod 700 /opt/services/traces \
    && rm -f /opt/services/seed/metrics.openmetrics

# Initialize Portal SQLite DB & Gitea repositories at build time, then snapshot clean state for reset
RUN bash /opt/services/initialize-state.sh

# Isolate backend DBs, seed truth, and service internals from agent user
RUN mkdir -p /workspace/output \
    && chown -R agent:agent /workspace \
    && chmod 700 /opt/services /var/lib/clearing-portal /var/lib/gitea /var/lib/mailpit /etc/grafana /etc/prometheus \
    && chmod 600 /etc/supervisor/conf.d/clearing.conf

ENTRYPOINT ["/opt/services/entrypoint.sh"]
CMD ["sleep", "infinity"]
