FROM python:3.11.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    sqlite3 \
    supervisor \
    unzip \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install pinned Python packages & Playwright Chromium (trimmed docs/cache to stay well under 2GB)
RUN pip install --no-cache-dir \
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
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Create non-root agent user
RUN useradd -m -s /bin/bash agent

# Install pinned lightweight open-source binaries (Gitea, Mailpit, Prometheus, Perfetto UI + trace_processor_shell, Grafana)
RUN mkdir -p /opt/bin /opt/perfetto-ui /opt/grafana /opt/services /var/lib/gitea /var/lib/mailpit /var/lib/prometheus/data /var/lib/grafana/data /var/lib/clearing-portal /var/log/supervisor /var/log/grafana \
    && curl -fsSL --retry 5 --retry-all-errors https://github.com/go-gitea/gitea/releases/download/v28.4.0/gitea-28.4.0-linux-amd64.xz -o /tmp/gitea.xz \
    && xz -d /tmp/gitea.xz && mv /tmp/gitea /opt/bin/gitea && chmod 755 /opt/bin/gitea \
    && curl -fsSL --retry 5 --retry-all-errors https://github.com/axllent/mailpit/releases/download/v1.34.0/mailpit-linux-amd64.tar.gz -o /tmp/mailpit.tar.gz \
    && tar -xzf /tmp/mailpit.tar.gz -C /opt/bin mailpit && rm -f /tmp/mailpit.tar.gz && chmod 755 /opt/bin/mailpit \
    && curl -fsSL --retry 5 --retry-all-errors https://github.com/prometheus/prometheus/releases/download/v3.16.0/prometheus-3.16.0.linux-amd64.tar.gz -o /tmp/prom.tar.gz \
    && tar -xzf /tmp/prom.tar.gz -C /tmp \
    && mv /tmp/prometheus-3.16.0.linux-amd64/prometheus /opt/bin/prometheus \
    && mv /tmp/prometheus-3.16.0.linux-amd64/promtool /opt/bin/promtool \
    && chmod 755 /opt/bin/prometheus /opt/bin/promtool && rm -rf /tmp/prom.tar.gz /tmp/prometheus-3.16.0.linux-amd64 \
    && curl -fsSL --retry 5 --retry-all-errors https://github.com/google/perfetto/releases/download/v59.1/perfetto-ui.zip -o /tmp/perfetto-ui.zip \
    && python3 -m zipfile -e /tmp/perfetto-ui.zip /opt/perfetto-ui && rm -f /tmp/perfetto-ui.zip \
    && curl -fsSL --retry 5 --retry-all-errors https://github.com/google/perfetto/releases/download/v59.1/linux-amd64.zip -o /tmp/perfetto-bins.zip \
    && python3 -m zipfile -e /tmp/perfetto-bins.zip /tmp/perfetto-bins \
    && mv /tmp/perfetto-bins/linux-amd64/trace_processor_shell /usr/local/bin/trace_processor_shell \
    && chmod 755 /usr/local/bin/trace_processor_shell && rm -rf /tmp/perfetto-bins.zip /tmp/perfetto-bins \
    && curl -fsSL --retry 5 --retry-all-errors https://github.com/grafana/grafana/releases/download/v13.3.2/grafana_13.3.2_44555861735_linux_amd64.deb -o /tmp/grafana.deb \
    && dpkg-deb -x /tmp/grafana.deb /opt/grafana && rm -f /tmp/grafana.deb

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
COPY seed/pftrace_encoder.py /opt/services/pftrace_encoder.py
COPY seed/generate_data.py /opt/services/generate_data.py
COPY seed/gitea_seed.py /opt/services/gitea_seed.py
COPY seed/mailpit_seed.py /opt/services/mailpit_seed.py
COPY supervisord.conf /etc/supervisor/conf.d/clearing.conf
COPY entrypoint.sh /opt/services/entrypoint.sh
COPY reset.sh /opt/services/reset.sh
RUN chmod +x /opt/services/entrypoint.sh /opt/services/reset.sh

# Generate frozen logs, binary .pftrace, OpenMetrics TSDB blocks, visual PNG/PDFs, contracts, and verifier truth
RUN python3 /opt/services/generate_data.py \
      --workspace-dir /workspace \
      --seed-dir /opt/services/seed \
      --truth-path /opt/services/ground_truth.json \
    && /opt/bin/promtool tsdb create-blocks-from openmetrics /opt/services/seed/metrics.openmetrics /var/lib/prometheus/data \
    && rm -f /opt/services/seed/metrics.openmetrics

# Initialize Portal SQLite DB & Gitea repositories at build time, then snapshot clean state for reset
RUN python3 -c "import sys; sys.path.insert(0, '/opt/services/portal'); import app; app.init_db(reset=True)" \
    && GITEA_WORK_DIR=/var/lib/gitea /opt/bin/gitea web --config /etc/gitea/app.ini >/tmp/gitea-build.log 2>&1 & \
    GITEA_PID=$!; \
    python3 /opt/services/gitea_seed.py \
      --gitea-bin /opt/bin/gitea \
      --gitea-config /etc/gitea/app.ini \
      --gitea-work /var/lib/gitea \
      --baseline-out /opt/services/gitea_baseline.json; \
    kill $GITEA_PID && wait $GITEA_PID || true; \
    rm -f /tmp/gitea-build.log; \
    mkdir -p /opt/services/clean_state/workspace /opt/services/clean_state/gitea /opt/services/clean_state/portal; \
    cp -a /workspace/. /opt/services/clean_state/workspace/; \
    cp -a /var/lib/gitea/. /opt/services/clean_state/gitea/; \
    cp -a /var/lib/clearing-portal/. /opt/services/clean_state/portal/

# Isolate backend DBs, seed truth, and service internals from agent user
RUN mkdir -p /workspace/output \
    && chown -R agent:agent /workspace \
    && chmod 700 /opt/services /var/lib/clearing-portal /var/lib/gitea /var/lib/mailpit

ENTRYPOINT ["/opt/services/entrypoint.sh"]
CMD ["sleep", "infinity"]
