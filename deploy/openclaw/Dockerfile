ARG OPENCLAW_BASE_IMAGE=alpine/openclaw:latest
FROM ${OPENCLAW_BASE_IMAGE}

ARG AGENT_REACH_ZIP_URL=https://github.com/Panniantong/agent-reach/archive/main.zip
ARG CLAWSEC_MONITOR_URL=https://raw.githubusercontent.com/chrisochrisochriso-cmyk/clawsec-monitor/main/clawsec-monitor.py
ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG BUSYBOX_URL=https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox

USER root

RUN set -eux; \
    openclaw plugins install @martian-engineering/lossless-claw; \
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py; \
    python3 /tmp/get-pip.py --break-system-packages; \
    python3 -m pip install --no-cache-dir --break-system-packages \
      --index-url "${PYPI_INDEX_URL}" \
      "${AGENT_REACH_ZIP_URL}" \
      "cryptography>=42.0.0"; \
    curl -fsSL "${CLAWSEC_MONITOR_URL}" \
      -o /usr/local/bin/clawsec-monitor.py; \
    chmod +x /usr/local/bin/clawsec-monitor.py; \
    python3 -m pip cache purge || true; \
    curl -fsSL "${BUSYBOX_URL}" -o /usr/local/bin/busybox; \
    chmod +x /usr/local/bin/busybox; \
    printf '#!/usr/bin/env sh\nexec /usr/local/bin/busybox crond \"$@\"\n' > /usr/local/bin/cron; \
    chmod +x /usr/local/bin/cron; \
    printf '#!/usr/bin/env sh\nexec /usr/local/bin/busybox crontab \"$@\"\n' > /usr/local/bin/crontab; \
    chmod +x /usr/local/bin/crontab; \
    mkdir -p /var/spool/cron/crontabs; \
    rm -f /tmp/get-pip.py

COPY bootstrap.sh /usr/local/bin/bootstrap.sh
COPY preset-skills /opt/openclaw/preset-skills
RUN chmod +x /usr/local/bin/bootstrap.sh

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]
