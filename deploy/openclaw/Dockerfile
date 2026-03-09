FROM alpine/openclaw:latest

USER root

RUN set -eux; \
    openclaw plugins install @martian-engineering/lossless-claw; \
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py; \
    python3 /tmp/get-pip.py --break-system-packages; \
    python3 -m pip install --no-cache-dir --break-system-packages \
      https://github.com/Panniantong/agent-reach/archive/main.zip; \
    python3 -m pip cache purge || true; \
    curl -fsSL https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox -o /usr/local/bin/busybox; \
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
