FROM hub.kce.ksyun.com/agentengine-public/openclaw:latest

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-pip cron \
 && python3 -m pip install --no-cache-dir --break-system-packages \
    https://github.com/Panniantong/agent-reach/archive/main.zip \
 && rm -rf /var/lib/apt/lists/*

COPY bootstrap.sh /usr/local/bin/bootstrap.sh
COPY preset-skills /opt/openclaw/preset-skills
RUN chmod +x /usr/local/bin/bootstrap.sh

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]
