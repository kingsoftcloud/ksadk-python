from pathlib import Path


def test_hermes_dockerfile_builds_and_copies_dashboard_assets():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "web_dist" in dockerfile
    assert "npm --workspace web run build" in dockerfile
    assert "tui_dist" in dockerfile
    assert "npm --workspace ui-tui run build" in dockerfile
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


def test_hermes_dockerfile_installs_hermes_from_selected_git_ref_and_cn_resilient_pip_defaults():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "ARG HERMES_NODE_BASE_IMAGE=node:20-bookworm-slim" in dockerfile
    assert "FROM ${HERMES_NODE_BASE_IMAGE} AS node_with_git" in dockerfile
    assert "FROM node_with_git AS hermes_src" in dockerfile
    assert "FROM hermes_src AS hermes_web" in dockerfile
    assert "FROM hermes_src AS hermes_tui" in dockerfile
    assert "COPY --from=hermes_src /src /tmp/hermes-src" in dockerfile
    assert "COPY --from=hermes_src /src/hermes_cli/dashboard_auth /usr/local/lib/python3.12/site-packages/hermes_cli/dashboard_auth" in dockerfile
    assert "COPY --from=hermes_web /src/hermes_cli/web_dist /usr/local/lib/python3.12/site-packages/hermes_cli/web_dist" in dockerfile
    assert "COPY --from=hermes_tui /src/ui-tui/dist/entry.js /usr/local/lib/python3.12/site-packages/hermes_cli/tui_dist/entry.js" in dockerfile
    assert '"/tmp/hermes-src[web,feishu,pty,cron,mcp,cli]"' in dockerfile
    assert '"hermes-agent[web,feishu,pty,cron,mcp,cli]"' not in dockerfile
    assert "cp -R /tmp/hermes-src/plugins/." not in dockerfile
    assert 'exec("""roots = [Path(p) / \\"plugins\\" for p in site.getsitepackages()]' in dockerfile
    assert 'for manifest_file in plugins_root.glob(\\"**/dashboard/manifest.json\\")' in dockerfile
    assert 'entry = str(manifest.get(\\"entry\\") or \\"\\").strip()' in dockerfile
    assert 'if entry_path.is_file():' in dockerfile
    assert 'Removed incomplete bundled dashboard plugin:' in dockerfile
    assert "rm -rf /tmp/hermes-src" in dockerfile
    assert "find /tmp/hermes-src/plugins" not in dockerfile
    assert "\\( -name plugin.yaml -o -name plugin.yml \\)" not in dockerfile
    assert "hermes-wpsxiezuo" in dockerfile
    assert "hermes-wpsxiezuo==" not in dockerfile
    assert '"discord.py[voice]==2.7.1"' in dockerfile
    assert '"brotlicffi==1.2.0.1"' in dockerfile
    assert '"aiohttp==3.13.3"' not in dockerfile
    assert '"websockets>=15.0.1,<16"' not in dockerfile
    assert '"cryptography>=46.0.5,<47"' not in dockerfile
    assert "qrcode>=" not in dockerfile
    assert "PIP_DEFAULT_TIMEOUT=180" in dockerfile
    assert "PIP_RETRIES=8" in dockerfile
    assert "PYPI_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
    assert "chromium ripgrep ffmpeg" in dockerfile


def test_hermes_dockerfile_smoke_imports_dashboard_server_after_plugin_copy():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert 'lazy_deps.feature_missing("platform.discord")' in dockerfile
    assert "Hermes dashboard dependency missing:" in dockerfile
    assert "Hermes web_dist missing:" in dockerfile
    assert "Hermes tui_dist missing:" in dockerfile
    assert "Hermes dashboard plugin assets missing:" in dockerfile
    assert 'kanban/dashboard/dist/index.js' in dockerfile
    assert 'hermes-achievements/dashboard/dist/index.js' in dockerfile
    assert "import hermes_cli.dashboard_auth.routes" in dockerfile
    assert 'm._make_tui_argv(Path("/missing-ui-tui"), False)' in dockerfile
    assert "import hermes_cli.web_server" in dockerfile
    assert "Hermes dashboard smoke import passed" in dockerfile
    assert "KSADK_HERMES_DASHBOARD_GATEWAY_ATTACH" in dockerfile
    assert "in-process gateway attach path currently trips a React TUI" in dockerfile
    assert "Hermes dashboard gateway attach patch target missing" in dockerfile


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


def test_hermes_dockerfile_pins_entrypoint_to_runtime_bootstrap():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]' in dockerfile
    assert 'ENTRYPOINT ["tini", "--"]' not in dockerfile
    assert "CMD []" in dockerfile
