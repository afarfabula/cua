FROM python:3.11.11-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|https\?://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY engine/sim_engine.c /tmp/sim_engine.c
RUN mkdir -p /out \
    && gcc -O2 -Wall -Wextra -s /tmp/sim_engine.c -lm -o /out/sim_engine \
    && chmod 700 /out/sim_engine

FROM python:3.11.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /workspace

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|https\?://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    procps \
    sqlite3 \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    pydantic==2.10.4 \
    requests==2.32.3 \
    pillow==11.1.0 \
    pytest==8.3.4 \
    python-multipart==0.0.20 \
    playwright==1.50.0 \
    && playwright install chromium --with-deps \
    && chmod -R a+rX /opt/ms-playwright \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN useradd -m -s /bin/bash agent \
    && mkdir -p /opt/services/engine /opt/services/seed /opt/services/clean_state/workspace \
       /opt/services/clean_state/db /var/lib/cluster-ops /var/log/supervisor /workspace/output

# Copy compiled stripped black-box simulator ONLY (no .c source in runtime image)
COPY --from=builder /out/sim_engine /opt/services/engine/sim_engine

# Copy services, seed script, and initial workspace files
COPY services/ /opt/services/services/
COPY seed/init_db.py /opt/services/seed/init_db.py
COPY workspace_init/ /workspace/
COPY supervisord.conf /etc/supervisor/conf.d/cluster.conf
COPY entrypoint.sh /opt/services/entrypoint.sh
COPY reset.sh /opt/services/reset.sh
RUN chmod +x /opt/services/entrypoint.sh /opt/services/reset.sh

# Seed initial SQLite DB + baseline + ground_truth, and capture clean_state for fast resets
RUN python3 /opt/services/seed/init_db.py \
    && cp -a /workspace/. /opt/services/clean_state/workspace/ \
    && cp -a /var/lib/cluster-ops/. /opt/services/clean_state/db/ \
    && chown -R agent:agent /workspace \
    && chmod 700 /opt/services /opt/services/engine /var/lib/cluster-ops \
    && chmod 600 /etc/supervisor/conf.d/cluster.conf

ENTRYPOINT ["/opt/services/entrypoint.sh"]
CMD ["sleep", "infinity"]
