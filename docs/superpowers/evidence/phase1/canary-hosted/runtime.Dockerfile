# Phase 1 Task 8 hosted-authority agent-kernel canary runtime image.
# Build context: phase1-agent-kernel worktree root.
#   docker build --platform linux/amd64 \
#     -t hub.kce.ksyun.com/agentengine/agent-kernel-canary:phase1-v4-<sha> \
#     -f docs/superpowers/evidence/phase1/canary-hosted/runtime.Dockerfile \
#     /Users/xiayu/kingsoft/code/agent-sdk/.worktrees/phase1-agent-kernel
FROM hub.kce.ksyun.com/bigdata-ai/python:3.12-slim
WORKDIR /app
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
COPY . /app
RUN pip install --no-cache-dir .
EXPOSE 8080
CMD ["python", "tests/phase1/canary_hosted_app.py"]
