from pathlib import Path


def test_hermes_dockerfile_builds_and_copies_dashboard_assets():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "web_dist" in dockerfile
    assert "npm run build" in dockerfile
    assert "mcporter" in dockerfile
    assert "/usr/local/lib/node_modules" in dockerfile
    assert "ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm" in dockerfile
    assert "ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx" in dockerfile
    assert "ln -sf ../lib/node_modules/mcporter/dist/cli.js /usr/local/bin/mcporter" in dockerfile


def test_hermes_dockerfile_recreates_js_cli_entrypoints_from_node_modules():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "npm install -g" in dockerfile
    assert "agent-browser@0.13.0 mcporter" in dockerfile
    assert "COPY --from=agent_browser /usr/local/lib/node_modules /usr/local/lib/node_modules" in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/mcporter /usr/local/bin/mcporter" not in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/npm /usr/local/bin/npm" not in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/npx /usr/local/bin/npx" not in dockerfile
    assert "ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack" in dockerfile
    assert "ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm" in dockerfile
    assert "ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx" in dockerfile
    assert "ln -sf ../lib/node_modules/mcporter/dist/cli.js /usr/local/bin/mcporter" in dockerfile
    assert "HOME=/home/node" in dockerfile
    assert "HERMES_HOME=/home/node/.hermes" in dockerfile
    assert "HERMES_STATE_DIR=/home/node/.hermes" in dockerfile
    assert "USER node" in dockerfile


def test_hermes_dockerfile_uses_local_source_install_and_cn_resilient_pip_defaults():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "FROM node:20-bookworm-slim AS hermes_src" in dockerfile
    assert "COPY --from=hermes_src /src /tmp/hermes-src" in dockerfile
    assert '"/tmp/hermes-src[web,feishu,pty,cron,mcp,cli]"' in dockerfile
    assert '"aiohttp>=3.13.3,<4"' in dockerfile
    assert '"websockets>=15.0.1,<16"' in dockerfile
    assert '"cryptography>=46.0.5,<47"' in dockerfile
    assert '"qrcode>=8.2,<9"' in dockerfile
    assert "PIP_DEFAULT_TIMEOUT=180" in dockerfile
    assert "PIP_RETRIES=8" in dockerfile
    assert "PYPI_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile


def test_hermes_dockerfile_bundles_runtime_common_from_repo_root():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "agentengine-runtime-common" not in dockerfile
    assert "COPY ksadk_runtime_common /opt/ksadk_runtime_common" in dockerfile
    assert "PYTHONPATH=/opt" in dockerfile
