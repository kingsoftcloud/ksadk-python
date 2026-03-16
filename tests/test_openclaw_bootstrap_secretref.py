import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "openclaw" / "bootstrap.sh"


def _build_base_env(state_dir: str, config_path: str) -> dict:
    env = os.environ.copy()
    safe_bin_dir = Path(state_dir) / "safe-bin"
    raw_bin_dir = Path(state_dir) / "bin"
    workspace_template_dir = Path(state_dir) / "workspace-template"
    safe_bin_dir.mkdir(parents=True, exist_ok=True)
    raw_bin_dir.mkdir(parents=True, exist_ok=True)
    workspace_template_dir.mkdir(parents=True, exist_ok=True)
    for cmd in ["pwd", "ls", "whoami", "id", "uname", "date", "ps", "df", "du", "stat", "find", "cat", "head", "tail", "wc", "git", "mcporter", "sh-safe", "bash-safe"]:
        wrapper_path = safe_bin_dir / cmd
        wrapper_path.write_text("#!/bin/sh\nexit 0\n")
        wrapper_path.chmod(0o755)
    for cmd in ["curl", "yt-dlp", "openclaw", "agent-reach", "gh", "xreach"]:
        raw_bin_path = raw_bin_dir / cmd
        raw_bin_path.write_text("#!/bin/sh\nexit 0\n")
        raw_bin_path.chmod(0o755)
    (workspace_template_dir / "SOUL.md").write_text("security soul\n")
    (workspace_template_dir / "AGENTS.md").write_text("security agents\n")
    (workspace_template_dir / "TOOLS.md").write_text("tool notes\n")
    env.pop("OPENCLAW_MODEL_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env["OPENCLAW_STATE_DIR"] = state_dir
    env["OPENCLAW_CONFIG_PATH"] = config_path
    env["OPENCLAW_BOOTSTRAP_ONLY"] = "1"
    env["OPENCLAW_MODEL_PROVIDER_ID"] = "ksyun"
    env["OPENCLAW_MODEL_BASE_URL"] = "http://example.test/v1"
    env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5"
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_WORKSPACE_TEMPLATE_DIR"] = str(workspace_template_dir)
    env["PATH"] = f"{raw_bin_dir}:{env['PATH']}"
    return env


def test_bootstrap_writes_secretref_for_model_api_key():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        secrets_path = Path(tmpdir) / "secrets.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        cfg = json.loads(config_path.read_text())
        assert cfg["models"]["providers"]["ksyun"]["apiKey"] == {
            "source": "file",
            "provider": "default",
            "id": "/providers/ksyun/apiKey",
        }
        assert cfg["secrets"]["providers"]["default"] == {
            "source": "file",
            "path": str(secrets_path),
            "mode": "json",
        }
        assert cfg["secrets"]["defaults"]["file"] == "default"
        assert json.loads(secrets_path.read_text()) == {
            "providers": {
                "ksyun": {
                    "apiKey": "dummy-secret-value",
                }
            }
        }
        assert secrets_path.stat().st_mode & 0o777 == 0o600


def test_bootstrap_keeps_env_secretref_when_explicitly_requested():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_MODEL_API_KEY_SECRET_SOURCE"] = "env"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        cfg = json.loads(config_path.read_text())
        assert cfg["models"]["providers"]["ksyun"]["apiKey"] == {
            "source": "env",
            "provider": "default",
            "id": "OPENCLAW_MODEL_API_KEY",
        }
        assert cfg["secrets"]["providers"]["default"]["source"] == "env"
        assert cfg["secrets"]["defaults"]["env"] == "default"


def test_bootstrap_fails_without_secret_env_value():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        combined = f"{result.stdout}\n{result.stderr}"
        assert "missing bootstrap secret env for file-backed model api key" in combined


def test_bootstrap_enforces_exec_approval_defaults():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        approvals_path = Path(tmpdir) / "exec-approvals.json"
        workspace_path = Path(tmpdir) / "workspace"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout

        cfg = json.loads(config_path.read_text())
        assert cfg["tools"]["fs"]["workspaceOnly"] is False
        assert cfg["tools"]["exec"]["host"] == "gateway"
        assert cfg["tools"]["exec"]["security"] == "allowlist"
        assert cfg["tools"]["exec"]["ask"] == "off"
        assert cfg["tools"]["exec"]["pathPrepend"][0] == str(Path(tmpdir) / "safe-bin")
        assert cfg["tools"]["elevated"]["enabled"] is False
        assert cfg["agents"]["defaults"]["workspace"] == str(workspace_path)

        approvals = json.loads(approvals_path.read_text())
        assert approvals["defaults"] == {
            "security": "allowlist",
            "ask": "off",
            "askFallback": "allowlist",
            "autoAllowSkills": False,
        }
        allowlist = approvals["agents"]["main"]["allowlist"]
        allowlist_basenames = {Path(entry["pattern"]).name for entry in allowlist}
        assert any(entry["pattern"] == str(Path(tmpdir) / "safe-bin" / "ls") for entry in allowlist)
        assert any(entry["pattern"] == str(Path(tmpdir) / "safe-bin" / "git") for entry in allowlist)
        assert any(entry["pattern"] == str(Path(tmpdir) / "safe-bin" / "mcporter") for entry in allowlist)
        assert any(entry["pattern"] == str(Path(tmpdir) / "safe-bin" / "sh-safe") for entry in allowlist)
        assert any(entry["pattern"] == str(Path(tmpdir) / "safe-bin" / "bash-safe") for entry in allowlist)
        assert "curl" in allowlist_basenames
        assert "yt-dlp" in allowlist_basenames
        assert "openclaw" in allowlist_basenames
        assert "agent-reach" in allowlist_basenames
        assert "gh" in allowlist_basenames
        assert "xreach" in allowlist_basenames
        assert (workspace_path / "SOUL.md").exists()
        assert (workspace_path / "AGENTS.md").exists()
        assert (workspace_path / "TOOLS.md").exists()


def test_bootstrap_merges_custom_exec_allowlist_patterns():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        approvals_path = Path(tmpdir) / "exec-approvals.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_EXEC_ALLOWLIST"] = "/opt/tools/read-only,/custom/bin/inspect"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        approvals = json.loads(approvals_path.read_text())
        patterns = {entry["pattern"] for entry in approvals["agents"]["main"]["allowlist"]}
        assert "/opt/tools/read-only" in patterns
        assert "/custom/bin/inspect" in patterns


def test_bootstrap_scrubs_model_api_key_from_gateway_process_env():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        captured_env_path = Path(tmpdir) / "gateway.env"
        fake_bin_dir = Path(tmpdir) / "bin"
        fake_node_path = fake_bin_dir / "node"
        fake_bin_dir.mkdir()
        fake_node_path.write_text(
            "#!/bin/sh\n"
            'printenv | sort > "${BOOTSTRAP_CAPTURE_ENV_PATH}"\n'
        )
        fake_node_path.chmod(0o755)

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["BOOTSTRAP_CAPTURE_ENV_PATH"] = str(captured_env_path)
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
        env.pop("OPENCLAW_BOOTSTRAP_ONLY", None)

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        captured_env = captured_env_path.read_text()
        assert "OPENCLAW_MODEL_API_KEY=" not in captured_env
        assert "OPENAI_API_KEY=" not in captured_env
        assert "OPENCLAW_INTERNAL_TRUSTED_PROXY_USER=openclaw-backend" in captured_env
        assert "OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER=x-forwarded-user" in captured_env


def test_bootstrap_runs_bundled_kdocs_setup_when_token_present():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        marker_path = Path(tmpdir) / "kdocs.marker"
        preset_skills_dir = Path(tmpdir) / "preset-skills" / "kdocs"
        preset_skills_dir.mkdir(parents=True, exist_ok=True)
        (preset_skills_dir / "setup.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"${KDOCS_TOKEN}\" > \"${OPENCLAW_KDOCS_MARKER_PATH}\"\n"
        )
        (preset_skills_dir / "setup.sh").chmod(0o755)
        (preset_skills_dir / "SKILL.md").write_text("kdocs skill\n")

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_PRESET_SKILLS_DIR"] = str(Path(tmpdir) / "preset-skills")
        env["OPENCLAW_PRESET_SKILLS_ALLOWLIST"] = "kdocs"
        env["OPENCLAW_KDOCS_MARKER_PATH"] = str(marker_path)
        env["KDOCS_TOKEN"] = "kdocs-test-token"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert marker_path.read_text() == "kdocs-test-token\n"
        assert (Path(tmpdir) / "skills" / "kdocs" / "setup.sh").exists()


def test_bootstrap_syncs_only_allowlisted_preset_skills():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        for skill_name in [
            "find-skills",
            "self-improving-agent",
            "kdocs",
            "agent-reach",
            "multi-search-engine",
            "tavily-search",
            "tuanziguardianclaw",
        ]:
            skill_dir = preset_skills_dir / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(f"{skill_name}\n")

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_PRESET_SKILLS_DIR"] = str(preset_skills_dir)

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        synced_skills = sorted(path.name for path in (Path(tmpdir) / "skills").iterdir() if path.is_dir())
        assert synced_skills == [
            "agent-reach",
            "find-skills",
            "kdocs",
            "multi-search-engine",
            "self-improving-agent",
            "tavily-search",
        ]


def test_bootstrap_enables_self_improvement_workspace_files():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills" / "self-improving-agent" / ".learnings"
        preset_skills_dir.mkdir(parents=True, exist_ok=True)
        (preset_skills_dir / "LEARNINGS.md").write_text("learning template\n")
        (preset_skills_dir / "ERRORS.md").write_text("error template\n")
        (preset_skills_dir / "FEATURE_REQUESTS.md").write_text("feature template\n")
        (preset_skills_dir.parent / "SKILL.md").write_text("self-improving-agent\n")

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_PRESET_SKILLS_DIR"] = str(Path(tmpdir) / "preset-skills")
        env["OPENCLAW_PRESET_SKILLS_ALLOWLIST"] = "self-improving-agent"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        workspace_learnings = Path(tmpdir) / "workspace" / ".learnings"
        assert (workspace_learnings / "LEARNINGS.md").read_text() == "learning template\n"
        assert (workspace_learnings / "ERRORS.md").read_text() == "error template\n"
        assert (workspace_learnings / "FEATURE_REQUESTS.md").read_text() == "feature template\n"


def test_bootstrap_patches_runtime_bundles_for_loopback_gateway_clients():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        gateway_bundle = dist_dir / "gateway-cli-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && params.sharedAuthOk && usesSharedSecretAuth;\n'
            '}\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\tif (!(params.token || params.password)) return true;\n'
            '\ttry {\n'
            '\t\tconst parsed = new URL(params.url);\n'
            '\t\treturn ![\n'
            '\t\t\t"127.0.0.1",\n'
            '\t\t\t"::1",\n'
            '\t\t\t"localhost"\n'
            '\t\t].includes(parsed.hostname);\n'
            '\t} catch {\n'
            '\t\treturn true;\n'
            '\t}\n'
            '}\n'
            'deviceIdentity: shouldAttachDeviceIdentityForGatewayCall({\n'
            '\t\t\t\turl,\n'
            '\t\t\t\ttoken,\n'
            '\t\t\t\tpassword\n'
            '\t\t\t}) ? loadOrCreateDeviceIdentity() : void 0,\n'
            'function ensureExplicitGatewayAuth(params) {\n'
            '\tif (!params.urlOverride) return;\n'
            '\tconst explicitToken = params.explicitAuth?.token;\n'
            '}\n'
            'if (!device && (!isControlUi || decision.kind !== "allow")) clearUnboundScopes();\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert 'internalTrustedProxyUser' in client_bundle.read_text()
        gateway_source = gateway_bundle.read_text()
        assert 'usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy"' in gateway_source
        assert 'function shouldAttachDeviceIdentityForGatewayCall(params) {' in gateway_source
        assert '].includes(parsed.hostname)) return false;' in gateway_source
        assert '}) ? loadOrCreateDeviceIdentity() : null,' in gateway_source
        assert 'const parsed = new URL(params.urlOverride);' in gateway_source
        assert 'if (["127.0.0.1", "::1", "localhost"].includes(parsed.hostname)) return;' in gateway_source
        assert 'const keepUnboundScopes = !device && decision.kind === "allow" && authMethod === "trusted-proxy" && !hasBrowserOriginHeader;' in gateway_source
        assert 'this.ws.addEventListener(`open`,()=>{this.lastSeq=null,this.queueConnect()})' in control_ui_bundle.read_text()


def test_bootstrap_defaults_state_dir_under_home_for_non_root_runtime():
    with TemporaryDirectory() as tmpdir:
        home_dir = Path(tmpdir) / "home" / "node"
        home_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.pop("OPENCLAW_MODEL_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENCLAW_STATE_DIR", None)
        env.pop("OPENCLAW_CONFIG_PATH", None)
        env["HOME"] = str(home_dir)
        env["OPENCLAW_BOOTSTRAP_ONLY"] = "1"
        env["OPENCLAW_MODEL_PROVIDER_ID"] = "ksyun"
        env["OPENCLAW_MODEL_BASE_URL"] = "http://example.test/v1"
        env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5"
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        state_dir = home_dir / ".openclaw"
        config_path = state_dir / "openclaw.json"
        secrets_path = state_dir / "secrets.json"

        assert result.returncode == 0, result.stderr or result.stdout
        assert config_path.exists()
        assert secrets_path.exists()
        cfg = json.loads(config_path.read_text())
        assert cfg["agents"]["defaults"]["workspace"] == str(state_dir / "workspace")
        assert cfg["secrets"]["providers"]["default"]["path"] == str(secrets_path)
