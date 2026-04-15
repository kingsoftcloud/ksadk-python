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
    assert "/usr/local/bin/npm" in dockerfile
    assert "/usr/local/bin/npx" in dockerfile
    assert "/usr/local/bin/mcporter" in dockerfile


def test_hermes_dockerfile_installs_node_toolchain_and_mcporter():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "npm install -g" in dockerfile
    assert "agent-browser@0.13.0 mcporter" in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/npm /usr/local/bin/npm" in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/npx /usr/local/bin/npx" in dockerfile
    assert "COPY --from=agent_browser /usr/local/bin/mcporter /usr/local/bin/mcporter" in dockerfile
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
    assert '"/tmp/hermes-src[web,messaging,feishu]"' in dockerfile
    assert "PIP_DEFAULT_TIMEOUT=180" in dockerfile
    assert "PIP_RETRIES=8" in dockerfile
    assert "PYPI_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
