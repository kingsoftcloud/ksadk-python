import json
import os
import subprocess
import time
import base64
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "openclaw" / "bootstrap.sh"
OPENCLAW_DOCKERFILE = REPO_ROOT / "deploy" / "openclaw" / "Dockerfile"
LATEST_OPENCLAW_BASE_IMAGE = (
    "ghcr.io/openclaw/openclaw:2026.6.1-slim@"
    "sha256:a83ee8716ab191534952299fe989374d75593aa9c7632c4e756e9d64b0ce8061"
)
VALID_MEM0_UUID = "e52b7fac-e641-4b34-b9f7-6b0b9f190cd4"


def _write_weixin_plugin_package_json(
    plugin_root: Path,
    *,
    version: str = "2.1.7",
    package_name: str = "@tencent-weixin/openclaw-weixin",
) -> None:
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "package.json").write_text(
        json.dumps(
            {
                "name": package_name,
                "version": version,
            }
        )
        + "\n"
    )


def _compute_directory_signature(dir_path: Path) -> str:
    result = subprocess.run(
        [
            "bash",
            "-lc",
            r'''dir_path="$1"
find "$dir_path" \( -type f -o -type l \) | LC_ALL=C sort | while IFS= read -r file_path; do
  rel_path="${file_path#"$dir_path/"}"
  if [[ -L "$file_path" ]]; then
    printf 'link\t%s\t%s\n' "$rel_path" "$(readlink "$file_path")"
    continue
  fi
  printf 'file\t%s\t' "$rel_path"
  cksum "$file_path" | awk '{print $1 "\t" $2}'
done | cksum | awk '{print $1 ":" $2}'
''',
            "_",
            str(dir_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _build_base_env(state_dir: str, config_path: str) -> dict:
    env = os.environ.copy()
    for key in (
        "OPENCLAW_DEFAULT_MODEL",
        "OPENAI_MODEL_NAME",
        "MODEL_NAME",
        "LLM_MODEL",
        "OPENCLAW_MODEL_CATALOG_JSON",
        "OPENCLAW_MODEL_PROVIDER_ID",
        "OPENCLAW_MODEL_BASE_URL",
        "OPENCLAW_MODEL_API",
        "OPENCLAW_MODEL_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "MODEL_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ):
        env.pop(key, None)
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
    for cmd in ["curl", "jq", "yt-dlp", "openclaw", "agent-browser", "gh", "xreach"]:
        raw_bin_path = raw_bin_dir / cmd
        raw_bin_path.write_text("#!/bin/sh\nexit 0\n")
        raw_bin_path.chmod(0o755)
    (workspace_template_dir / "SOUL.md").write_text("security soul\n")
    (workspace_template_dir / "AGENTS.md").write_text("security agents\n")
    (workspace_template_dir / "MEMORY.md").write_text("persistent memory\n")
    (workspace_template_dir / "USER.MD").write_text("user preferences\n")
    (workspace_template_dir / "TOOLS.md").write_text("tool notes\n")
    env.pop("OPENCLAW_MODEL_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env["HOME"] = state_dir
    env["OPENCLAW_STATE_DIR"] = state_dir
    env["OPENCLAW_CONFIG_PATH"] = config_path
    env["OPENCLAW_BOOTSTRAP_ONLY"] = "1"
    env["OPENCLAW_MODEL_PROVIDER_ID"] = "ksyun"
    env["OPENCLAW_MODEL_BASE_URL"] = "http://example.test/v1"
    env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5.1"
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_WORKSPACE_TEMPLATE_DIR"] = str(workspace_template_dir)
    env["PATH"] = f"{raw_bin_dir}:{env['PATH']}"
    return env


def _build_mem0_manifest_json() -> str:
    return json.dumps(
        {
            "schema_version": "v1",
            "backend_type": "mem0",
            "config": {
                "mem0_instance_id": VALID_MEM0_UUID,
                "mem0_region": "cn-qingyangtest-1",
            },
            "secrets_env": {
                "api_key": "MEM0_API_KEY",
                "user_id": "MEM0_USER_ID",
                "base_url": "MEM0_BASE_URL",
            },
        }
    )


def _build_openclaw_default_memory_manifest_json() -> str:
    return json.dumps(
        {
            "schema_version": "v1",
            "backend_type": "openclaw_default",
        }
    )


def _assert_model_token_defaults(models: list[dict], *, minimum_max_tokens: int = 20000) -> None:
    for model in models:
        if "contextWindow" not in model and "maxTokens" not in model:
            continue
        assert model["contextWindow"] == 200000
        assert model["maxTokens"] >= minimum_max_tokens


def test_openclaw_dockerfile_tracks_latest_official_channel_plugins():
    dockerfile = OPENCLAW_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        f"ARG OPENCLAW_BASE_IMAGE={LATEST_OPENCLAW_BASE_IMAGE}"
        in dockerfile
    )
    assert "ARG OPENCLAW_WEIXIN_PLUGIN_SPEC=@tencent-weixin/openclaw-weixin" in dockerfile
    assert "ARG OPENCLAW_LARK_PLUGIN_SPEC=@larksuite/openclaw-lark" in dockerfile
    assert "ARG OPENCLAW_MEM0_PLUGIN_ID=openclaw-mem0" in dockerfile
    assert "ARG OPENCLAW_MEM0_PLUGIN_URL=https://memory-engine.ks3-cn-beijing.ksyuncs.com/ksc-openclaw-mem0-1.0.6.tgz" in dockerfile
    assert "ksc-openclaw-mem0-1.1." not in dockerfile
    assert "ARG OPENCLAW_INSTALL_WPS_XIEZUO_PLUGIN=true" in dockerfile
    assert "ARG OPENCLAW_WPS_XIEZUO_PLUGIN_SPEC=@wps365/openclaw-wpsxiezuo" in dockerfile
    assert "ARG OPENCLAW_WPS_XIEZUO_PLUGIN_ID=wps-xiezuo" in dockerfile
    assert "ARG OPENCLAW_INSTALL_DIAGNOSTICS_OTEL_PLUGIN=true" in dockerfile
    assert "ARG OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_SPEC=@openclaw/diagnostics-otel" in dockerfile
    assert "ARG OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_ID=diagnostics-otel" in dockerfile
    assert "openclaw-wps-xiezuo-1.6.0.tgz" not in dockerfile
    assert "deploy/openclaw/wps-xiezuo-assets" not in dockerfile
    assert 'install_default_plugin "${OPENCLAW_WPS_XIEZUO_PLUGIN_SPEC}" "${OPENCLAW_WPS_XIEZUO_PLUGIN_ID}"' in dockerfile


def test_openclaw_dockerfile_moves_apt_archives_out_of_var_cache():
    dockerfile = OPENCLAW_DOCKERFILE.read_text(encoding="utf-8")

    assert "mkdir -p /tmp/apt-cache" in dockerfile
    assert "Dir::Cache::archives=/tmp/apt-cache" in dockerfile
    assert "rm -rf /var/lib/apt/lists/* /tmp/apt-cache" in dockerfile


def test_openclaw_dockerfile_strips_workspace_dev_dependencies_before_plugin_install():
    dockerfile = OPENCLAW_DOCKERFILE.read_text(encoding="utf-8")

    assert 'spec.startsWith("workspace:")' in dockerfile
    assert "delete deps[name]" in dockerfile
    assert 'npm install --omit=dev --no-audit --no-fund --registry "${NPM_REGISTRY}"' in dockerfile
    assert 'ln -s /app "${src_dir}/node_modules/openclaw"' in dockerfile


def test_openclaw_dockerfile_installs_local_plugin_archives_without_force_flag_for_2026_3_28_compatibility():
    dockerfile = OPENCLAW_DOCKERFILE.read_text(encoding="utf-8")

    assert "compatible with upstream OpenClaw 2026.3.28" in dockerfile
    assert 'openclaw plugins install "${archive_path}"; \\' in dockerfile
    assert 'openclaw plugins install "${archive_path}" --force; \\' not in dockerfile


def test_openclaw_runtime_bundles_runtime_common_and_manifest_renderer():
    dockerfile = OPENCLAW_DOCKERFILE.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert "COPY ksadk_runtime_common /opt/ksadk_runtime_common" in dockerfile
    assert "COPY deploy/openclaw/workspace_files_app.py /opt/openclaw/workspace_files_app.py" in dockerfile
    assert '"fastapi>=0.100.0,<0.124.0"' in dockerfile
    assert '"httpx>=0.24.0,<1.0.0"' in dockerfile
    assert '"uvicorn>=0.23.0,<1.0.0"' in dockerfile
    assert '"websockets>=11.0.0,<16.0.0"' in dockerfile
    assert '"python-multipart>=0.0.9,<1.0.0"' in dockerfile
    assert "PYTHONPATH=/opt" in dockerfile
    assert "from ksadk_runtime_common.memory_backend.render import render_to_json" in bootstrap
    assert 'uvicorn workspace_files_app:app \\' in bootstrap
    assert 'OPENCLAW_WORKSPACE_FILES_PROXY_URL' in bootstrap


def test_openclaw_dockerfile_clones_official_kdocs_skill_and_normalizes_runtime_layout():
    dockerfile = OPENCLAW_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG KDOCS_SKILL_REPO=https://github.com/kdocs-app/kdocs-skill.git" in dockerfile
    assert 'git clone --depth 1 "${KDOCS_SKILL_REPO}" /tmp/kdocs-skill' in dockerfile
    assert 'mkdir -p /opt/openclaw/preset-skills/kdocs/scripts' in dockerfile
    assert 'cp -R /tmp/kdocs-skill/. /opt/openclaw/preset-skills/kdocs/' in dockerfile
    assert 'printf \'%s\\n\' \\' in dockerfile
    assert 'exec bash "${SCRIPT_DIR}/scripts/setup.sh" "$@"' in dockerfile


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


def test_bootstrap_applies_openclaw_config_patch_json():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "plugins": {
                        "allow": ["existing-plugin"],
                        "entries": {"existing-plugin": {"enabled": True}},
                    },
                    "diagnostics": {"enabled": False},
                }
            )
            + "\n"
        )
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_CONFIG_PATCH_JSON"] = json.dumps(
            {
                "plugins": {
                    "allow": ["diagnostics-otel"],
                    "entries": {"diagnostics-otel": {"enabled": True}},
                },
                "diagnostics": {
                    "enabled": True,
                    "otel": {
                        "enabled": True,
                        "endpoint": "https://langfuse.pre.example.com/api/public/otel",
                        "protocol": "http/protobuf",
                        "serviceName": "agentengine-openclaw-demo",
                        "traces": True,
                        "metrics": False,
                        "logs": False,
                        "captureContent": False,
                    },
                },
            }
        )

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
        assert cfg["plugins"]["entries"]["existing-plugin"]["enabled"] is True
        assert cfg["plugins"]["entries"]["diagnostics-otel"]["enabled"] is True
        assert "diagnostics-otel" in cfg["plugins"]["allow"]
        assert cfg["diagnostics"]["enabled"] is True
        assert cfg["diagnostics"]["otel"] == {
            "enabled": True,
            "endpoint": "https://langfuse.pre.example.com/api/public/otel",
            "protocol": "http/protobuf",
            "serviceName": "agentengine-openclaw-demo",
            "traces": True,
            "metrics": False,
            "logs": False,
            "captureContent": False,
        }


def test_bootstrap_migrates_legacy_diagnostics_capture_content_to_otel():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_CONFIG_PATCH_JSON"] = json.dumps(
            {
                "diagnostics": {
                    "enabled": True,
                    "captureContent": False,
                    "otel": {
                        "enabled": True,
                        "endpoint": "https://langfuse.pre.example.com/api/public/otel",
                        "protocol": "http/protobuf",
                    },
                }
            }
        )

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
        assert cfg["diagnostics"]["enabled"] is True
        assert "captureContent" not in cfg["diagnostics"]
        assert cfg["diagnostics"]["otel"]["captureContent"] is False


def test_bootstrap_enables_diagnostics_otel_for_langfuse_env():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["LANGFUSE_PUBLIC_KEY"] = "pk-test"
        env["LANGFUSE_SECRET_KEY"] = "sk-test"
        env["LANGFUSE_BASE_URL"] = "https://langfuse.pre.example.com/"
        env["OTEL_SERVICE_NAME"] = "openclaw-langfuse-e2e"

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
        expected_auth = base64.b64encode(b"pk-test:sk-test").decode("ascii")
        assert cfg["plugins"]["entries"]["diagnostics-otel"]["enabled"] is True
        assert "diagnostics-otel" in cfg["plugins"]["allow"]
        assert cfg["diagnostics"]["enabled"] is True
        assert cfg["diagnostics"]["otel"] == {
            "enabled": True,
            "endpoint": "https://langfuse.pre.example.com/api/public/otel",
            "protocol": "http/protobuf",
            "serviceName": "openclaw-langfuse-e2e",
            "traces": True,
            "metrics": False,
            "logs": False,
            "headers": {
                "Authorization": f"Basic {expected_auth}",
                "x-langfuse-ingestion-version": "4",
            },
        }


def test_bootstrap_maps_gateway_token_to_shared_secret_when_token_mode_enabled():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_GATEWAY_AUTH_MODE"] = "token"
        env["OPENCLAW_GATEWAY_TOKEN"] = "gateway-token-demo"

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
        assert cfg["gateway"]["auth"]["mode"] == "token"
        assert cfg["gateway"]["auth"]["password"] == "gateway-token-demo"


def test_bootstrap_enables_openresponses_http_endpoint_by_default():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        assert cfg["gateway"]["http"]["endpoints"]["responses"]["enabled"] is True


def test_bootstrap_respects_explicit_openresponses_http_endpoint_disable():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "gateway": {
                        "http": {
                            "endpoints": {
                                "responses": {
                                    "enabled": False,
                                }
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
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
        assert cfg["gateway"]["http"]["endpoints"]["responses"]["enabled"] is False


def test_bootstrap_clears_stale_gateway_shared_secret_when_mode_returns_to_trusted_proxy():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "gateway": {
                        "auth": {
                            "mode": "token",
                            "password": "stale-secret",
                        }
                    }
                }
            )
        )
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_GATEWAY_AUTH_MODE"] = "trusted-proxy"
        env["OPENCLAW_GATEWAY_TOKEN"] = "stale-secret"
        env["OPENCLAW_GATEWAY_PASSWORD"] = "stale-secret"

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
        assert cfg["gateway"]["auth"]["mode"] == "trusted-proxy"
        assert "password" not in (cfg.get("gateway", {}).get("auth", {}))


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


def test_bootstrap_keeps_model_env_fallbacks_for_background_runs():
    source = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert "false missing-auth failures against auth-profiles.json" in source
    assert "unset OPENCLAW_MODEL_API_KEY OPENAI_API_KEY LLM_API_KEY MODEL_API_KEY" not in source


def test_bootstrap_defaults_heartbeat_to_isolated_light_context():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        assert cfg["agents"]["defaults"]["heartbeat"]["every"] == "30m"
        assert cfg["agents"]["defaults"]["heartbeat"]["target"] == "none"
        assert cfg["agents"]["defaults"]["heartbeat"]["isolatedSession"] is True
        assert cfg["agents"]["defaults"]["heartbeat"]["lightContext"] is True


def test_bootstrap_disables_exec_notify_on_exit_by_default():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        assert cfg["tools"]["exec"]["notifyOnExit"] is False
        assert cfg["tools"]["exec"]["notifyOnExitEmptySuccess"] is False


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


def test_bootstrap_does_not_keep_gateway_password_when_not_in_token_mode():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        assert cfg["gateway"]["auth"]["mode"] == "trusted-proxy"
        assert "password" not in cfg["gateway"]["auth"]


def test_bootstrap_defaults_dual_ksyun_catalog_when_unspecified():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env.pop("OPENCLAW_MODEL_CATALOG_JSON", None)

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "ksyun/glm-5.1"
        assert cfg["agents"]["defaults"]["model"]["fallbacks"] == ["ksyun/kimi-k2.6"]
        assert cfg["agents"]["defaults"]["imageModel"]["primary"] == "ksyun/kimi-k2.6"
        models = cfg["models"]["providers"]["ksyun"]["models"]
        assert [item["id"] for item in models] == ["glm-5.1", "kimi-k2.6"]
        assert models[0]["input"] == ["text"]
        assert models[1]["input"] == ["text", "image"]
        _assert_model_token_defaults(models)
        selectable = cfg["agents"]["defaults"]["models"]
        assert "ksyun/glm-5.1" in selectable
        assert "ksyun/kimi-k2.6" in selectable


def test_bootstrap_global_model_preference_keeps_dual_ksyun_catalog():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENAI_MODEL_NAME"] = "glm-5.1"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env.pop("OPENCLAW_MODEL_CATALOG_JSON", None)

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "ksyun/glm-5.1"
        assert cfg["agents"]["defaults"]["model"]["fallbacks"] == ["ksyun/kimi-k2.6"]
        assert cfg["agents"]["defaults"]["imageModel"]["primary"] == "ksyun/kimi-k2.6"
        models = cfg["models"]["providers"]["ksyun"]["models"]
        assert [item["id"] for item in models] == ["glm-5.1", "kimi-k2.6"]
        assert models[0]["input"] == ["text"]
        assert models[1]["input"] == ["text", "image"]
        _assert_model_token_defaults(models)


def test_bootstrap_openclaw_default_model_alias_keeps_dual_catalog():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5.1"
        env.pop("OPENCLAW_MODEL_CATALOG_JSON", None)

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "ksyun/glm-5.1"
        assert cfg["agents"]["defaults"]["model"]["fallbacks"] == ["ksyun/kimi-k2.6"]
        assert cfg["agents"]["defaults"]["imageModel"]["primary"] == "ksyun/kimi-k2.6"
        models = cfg["models"]["providers"]["ksyun"]["models"]
        assert [item["id"] for item in models] == ["glm-5.1", "kimi-k2.6"]
        assert models[0]["input"] == ["text"]
        assert models[1]["input"] == ["text", "image"]
        _assert_model_token_defaults(models)
        selectable = cfg["agents"]["defaults"]["models"]
        assert "ksyun/glm-5.1" in selectable
        assert "ksyun/kimi-k2.6" in selectable


def test_bootstrap_preserves_existing_defaults_model_fallbacks_and_image_model():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": {
                                "primary": "ksyun/deepseek-v3",
                                "fallbacks": ["ksyun/glm-5.1"],
                            },
                            "imageModel": {
                                "primary": "ksyun/kimi-k2.6",
                            },
                        }
                    }
                }
            )
        )
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5.1"
        env.pop("OPENCLAW_MODEL_CATALOG_JSON", None)

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "ksyun/deepseek-v3"
        assert cfg["agents"]["defaults"]["model"]["fallbacks"] == ["ksyun/glm-5.1"]
        assert cfg["agents"]["defaults"]["imageModel"]["primary"] == "ksyun/kimi-k2.6"


def test_bootstrap_prefers_glm51_as_default_primary_when_catalog_is_present():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env.pop("OPENAI_MODEL_NAME", None)
        env["OPENCLAW_MODEL_CATALOG_JSON"] = (
            '[{"id":"kimi-k2.6"},{"id":"glm-5.1"}]'
        )

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "ksyun/glm-5.1"
        models = cfg["models"]["providers"]["ksyun"]["models"]
        assert [item["id"] for item in models] == ["kimi-k2.6", "glm-5.1"]


def test_bootstrap_appends_primary_model_when_default_catalog_does_not_include_it():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENAI_MODEL_NAME"] = "ksyun/deepseek-v3"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env.pop("OPENCLAW_MODEL_CATALOG_JSON", None)

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "ksyun/deepseek-v3"
        models = cfg["models"]["providers"]["ksyun"]["models"]
        assert [item["id"] for item in models] == ["glm-5.1", "kimi-k2.6", "deepseek-v3"]
        _assert_model_token_defaults(models)


def test_bootstrap_qualifies_namespaced_model_selection_refs_when_provider_differs_from_prefix():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_MODEL_PROVIDER_ID"] = "hanhai"
        env["OPENAI_MODEL_NAME"] = "Qzhou/glm-5"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env["OPENCLAW_MODEL_CATALOG_JSON"] = json.dumps(
            [
                {
                    "id": "Qzhou/glm-5",
                    "name": "glm-5",
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": ["text"],
                },
                {
                    "id": "Qzhou/kimi-k2.6",
                    "name": "kimi-k2.6",
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": ["text", "image"],
                },
            ]
        )

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "hanhai/Qzhou/glm-5"
        assert cfg["agents"]["defaults"]["model"]["fallbacks"] == ["hanhai/Qzhou/kimi-k2.6"]
        assert cfg["agents"]["defaults"]["imageModel"]["primary"] == "hanhai/Qzhou/kimi-k2.6"
        models = cfg["models"]["providers"]["hanhai"]["models"]
        assert [item["id"] for item in models] == ["Qzhou/glm-5", "Qzhou/kimi-k2.6"]
        selectable = cfg["agents"]["defaults"]["models"]
        assert sorted(selectable) == ["hanhai/Qzhou/glm-5", "hanhai/Qzhou/kimi-k2.6"]


def test_bootstrap_qualifies_namespaced_model_selection_refs_without_catalog_when_provider_differs():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_MODEL_PROVIDER_ID"] = "hanhai"
        env["OPENAI_MODEL_NAME"] = "Qzhou/glm-5"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env.pop("OPENCLAW_MODEL_CATALOG_JSON", None)

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "hanhai/Qzhou/glm-5"
        assert "fallbacks" not in cfg["agents"]["defaults"]["model"]
        models = cfg["models"]["providers"]["hanhai"]["models"]
        assert [item["id"] for item in models] == ["Qzhou/glm-5"]
        selectable = cfg["agents"]["defaults"]["models"]
        assert sorted(selectable) == ["hanhai/Qzhou/glm-5"]


def test_bootstrap_migrates_legacy_namespaced_model_selection_refs_for_custom_provider():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": {
                                "primary": "Qzhou/glm-5",
                                "fallbacks": ["Qzhou/kimi-k2.6"],
                            },
                            "imageModel": {
                                "primary": "Qzhou/kimi-k2.6",
                            },
                            "models": {
                                "Qzhou/glm-5": {},
                                "Qzhou/kimi-k2.6": {},
                            },
                        }
                    },
                    "models": {
                        "providers": {
                            "hanhai": {
                                "models": [
                                    {
                                        "id": "Qzhou/glm-5",
                                        "name": "glm-5",
                                        "api": "openai-completions",
                                        "reasoning": False,
                                        "input": ["text"],
                                    },
                                    {
                                        "id": "Qzhou/kimi-k2.6",
                                        "name": "kimi-k2.6",
                                        "api": "openai-completions",
                                        "reasoning": False,
                                        "input": ["text", "image"],
                                    },
                                ]
                            }
                        }
                    },
                }
            )
        )
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_MODEL_PROVIDER_ID"] = "hanhai"
        env["OPENAI_MODEL_NAME"] = "Qzhou/glm-5"
        env.pop("OPENCLAW_DEFAULT_MODEL", None)
        env["OPENCLAW_MODEL_CATALOG_JSON"] = json.dumps(
            [
                {
                    "id": "Qzhou/glm-5",
                    "name": "glm-5",
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": ["text"],
                },
                {
                    "id": "Qzhou/kimi-k2.6",
                    "name": "kimi-k2.6",
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": ["text", "image"],
                },
            ]
        )

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
        assert cfg["agents"]["defaults"]["model"]["primary"] == "hanhai/Qzhou/glm-5"
        assert cfg["agents"]["defaults"]["model"]["fallbacks"] == ["hanhai/Qzhou/kimi-k2.6"]
        assert cfg["agents"]["defaults"]["imageModel"]["primary"] == "hanhai/Qzhou/kimi-k2.6"
        assert sorted(cfg["agents"]["defaults"]["models"]) == [
            "hanhai/Qzhou/glm-5",
            "hanhai/Qzhou/kimi-k2.6",
        ]
        models = cfg["models"]["providers"]["hanhai"]["models"]
        assert [item["id"] for item in models] == ["Qzhou/glm-5", "Qzhou/kimi-k2.6"]


def test_bootstrap_disables_builtin_web_search_by_default():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        assert cfg["tools"]["web"]["search"]["enabled"] is False
        assert cfg["tools"]["web"]["fetch"]["enabled"] is False
        assert "provider" not in cfg["tools"]["web"]["search"]
        assert "plugins" not in cfg or "perplexity" not in cfg.get("plugins", {}).get("entries", {})


def test_bootstrap_cleans_up_legacy_auto_builtin_web_search():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "tools": {
                        "web": {
                            "search": {
                                "enabled": True,
                                "provider": "perplexity",
                            }
                        }
                    },
                    "plugins": {
                        "entries": {
                            "perplexity": {
                                "config": {
                                    "webSearch": {
                                        "baseUrl": "http://example.test/v1",
                                        "model": "deepseek-v3.2",
                                        "apiKey": {
                                            "source": "file",
                                            "provider": "default",
                                            "id": "/providers/ksyun/apiKey",
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            )
        )
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
        assert cfg["tools"]["web"]["search"]["enabled"] is False
        assert "provider" not in cfg["tools"]["web"]["search"]
        assert "plugins" not in cfg or "perplexity" not in cfg.get("plugins", {}).get("entries", {})


def test_bootstrap_preserves_explicit_builtin_web_search_provider():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "tools": {
                        "web": {
                            "search": {
                                "enabled": True,
                                "provider": "brave",
                            }
                        }
                    }
                }
            )
        )
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
        assert cfg["tools"]["web"]["search"]["enabled"] is True
        assert cfg["tools"]["web"]["search"]["provider"] == "brave"
        assert "plugins" not in cfg or "perplexity" not in cfg.get("plugins", {}).get("entries", {})


def test_bootstrap_disables_legacy_builtin_web_fetch_for_default_ksyun_runtime():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "tools": {
                        "web": {
                            "fetch": {
                                "enabled": True,
                            }
                        }
                    }
                }
            )
        )
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_MODEL_BASE_URL"] = "https://kspmas.ksyun.com/v1/"

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
        assert cfg["tools"]["web"]["fetch"]["enabled"] is False


def test_bootstrap_preserves_explicit_builtin_web_fetch_enablement_via_env():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_WEB_FETCH_ENABLED"] = "true"

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
        assert cfg["tools"]["web"]["fetch"]["enabled"] is True


def test_bootstrap_enables_builtin_browser_by_default():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        assert cfg["browser"]["enabled"] is True
        assert cfg["browser"]["headless"] is True
        assert cfg["browser"]["noSandbox"] is True
        assert cfg["browser"]["ssrfPolicy"] == {"dangerouslyAllowPrivateNetwork": True}


def test_bootstrap_preserves_explicit_builtin_browser_enablement_in_config():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "browser": {
                        "enabled": True,
                        "headless": False,
                        "noSandbox": False,
                        "ssrfPolicy": {
                            "dangerouslyAllowPrivateNetwork": False,
                            "hostnameAllowlist": ["docs.example.com"],
                        },
                    }
                }
            )
        )
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
        assert cfg["browser"]["enabled"] is True
        assert cfg["browser"]["headless"] is False
        assert cfg["browser"]["noSandbox"] is False
        assert cfg["browser"]["ssrfPolicy"] == {
            "dangerouslyAllowPrivateNetwork": False,
            "hostnameAllowlist": ["docs.example.com"],
        }


def test_bootstrap_allows_reenabling_builtin_browser_via_env():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_BROWSER_ENABLED"] = "true"

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
        assert cfg["browser"]["enabled"] is True


def test_bootstrap_allows_disabling_builtin_browser_via_env():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_BROWSER_ENABLED"] = "false"

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
        assert cfg["browser"]["enabled"] is False


def test_bootstrap_keeps_browser_ssrf_policy_strict_in_strict_mode():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_EXEC_STRICT_MODE"] = "true"

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
        assert "ssrfPolicy" not in cfg["browser"]


def test_bootstrap_recovers_from_blank_secret_ref_env_overrides():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_MODEL_API_KEY_SECRET_SOURCE"] = " file "
        env["OPENCLAW_MODEL_API_KEY_SECRET_PROVIDER"] = " default "
        env["OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH"] = " "
        env["OPENCLAW_MODEL_API_KEY_SECRET_ID"] = " "

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
        assert cfg["secrets"]["providers"]["default"] == {
            "source": "file",
            "path": str(Path(tmpdir) / "secrets.json"),
            "mode": "json",
        }
        assert cfg["models"]["providers"]["ksyun"]["apiKey"] == {
            "source": "file",
            "provider": "default",
            "id": "/providers/ksyun/apiKey",
        }


def test_bootstrap_syncs_kdocs_by_default_without_token():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
            "tavily-search",
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
        synced_skills = sorted(
            path.name for path in (Path(tmpdir) / "skills").iterdir() if path.is_dir()
        )
        assert synced_skills == [
            "agent-browser-clawdbot",
            "clawhub-store",
            "kdocs",
        ]


def test_bootstrap_removes_previously_synced_removed_preset_skill_when_unchanged():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        skills_dir = Path(tmpdir) / "skills"
        managed_find_skills_dir = skills_dir / "find-skills"
        managed_find_skills_dir.mkdir(parents=True, exist_ok=True)
        (managed_find_skills_dir / "SKILL.md").write_text("legacy managed find-skills\n")

        cache_dir = Path(tmpdir) / ".bootstrap-cache" / "preset-skills"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "find-skills.sig").write_text(
            _compute_directory_signature(managed_find_skills_dir) + "\n"
        )

        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
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
        assert not managed_find_skills_dir.exists()
        assert not (cache_dir / "find-skills.sig").exists()


def test_bootstrap_preserves_user_managed_removed_preset_skill():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        user_find_skills_dir = Path(tmpdir) / "skills" / "find-skills"
        user_find_skills_dir.mkdir(parents=True, exist_ok=True)
        (user_find_skills_dir / "SKILL.md").write_text("custom user find-skills\n")
        (user_find_skills_dir / "README.md").write_text("owned-by-user\n")

        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
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
        assert (user_find_skills_dir / "SKILL.md").read_text() == "custom user find-skills\n"
        assert (user_find_skills_dir / "README.md").read_text() == "owned-by-user\n"
        assert "preserved user-managed skill find-skills" in result.stderr


def test_bootstrap_removes_previously_synced_multi_search_skill_when_no_longer_default():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        skills_dir = Path(tmpdir) / "skills"
        managed_multi_search_dir = skills_dir / "multi-search-engine"
        managed_multi_search_dir.mkdir(parents=True, exist_ok=True)
        (managed_multi_search_dir / "SKILL.md").write_text("legacy managed multi-search-engine\n")

        cache_dir = Path(tmpdir) / ".bootstrap-cache" / "preset-skills"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "multi-search-engine.sig").write_text(
            _compute_directory_signature(managed_multi_search_dir) + "\n"
        )

        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
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
        assert not managed_multi_search_dir.exists()
        assert not (cache_dir / "multi-search-engine.sig").exists()


def test_bootstrap_removes_legacy_multi_search_skill_by_source_match_without_sig():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        skills_dir = Path(tmpdir) / "skills"
        bundled_multi_search_dir = preset_skills_dir / "multi-search-engine"
        managed_multi_search_dir = skills_dir / "multi-search-engine"

        bundled_multi_search_dir.mkdir(parents=True, exist_ok=True)
        (bundled_multi_search_dir / "SKILL.md").write_text("legacy bundled multi-search-engine\n")
        managed_multi_search_dir.mkdir(parents=True, exist_ok=True)
        (managed_multi_search_dir / "SKILL.md").write_text("legacy bundled multi-search-engine\n")

        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
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
        assert not managed_multi_search_dir.exists()
        assert "removed deprecated bundled skill multi-search-engine (legacy source match)" in result.stderr


def test_bootstrap_syncs_multi_search_skill_when_explicitly_allowlisted():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
            "multi-search-engine",
        ]:
            skill_dir = preset_skills_dir / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(f"{skill_name}\n")

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_PRESET_SKILLS_DIR"] = str(preset_skills_dir)
        env["OPENCLAW_PRESET_SKILLS_ALLOWLIST"] = (
            "clawhub-store,agent-browser-clawdbot,kdocs,multi-search-engine"
        )

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        synced_skills = sorted(
            path.name for path in (Path(tmpdir) / "skills").iterdir() if path.is_dir()
        )
        assert synced_skills == [
            "agent-browser-clawdbot",
            "clawhub-store",
            "kdocs",
            "multi-search-engine",
        ]


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
        assert cfg["tools"]["exec"]["security"] == "full"
        assert cfg["tools"]["exec"]["ask"] == "off"
        assert "pathPrepend" not in cfg["tools"]["exec"]
        assert cfg["tools"]["elevated"]["enabled"] is False
        assert cfg["agents"]["defaults"]["workspace"] == str(workspace_path)

        approvals = json.loads(approvals_path.read_text())
        assert approvals["defaults"] == {
            "security": "full",
            "ask": "off",
            "askFallback": "full",
            "autoAllowSkills": False,
        }
        assert "agents" not in approvals or "main" not in approvals.get("agents", {})
        assert not (workspace_path / "SOUL.md").exists()
        assert not (workspace_path / "AGENTS.md").exists()
        assert not (workspace_path / "MEMORY.md").exists()
        assert not (workspace_path / "USER.MD").exists()
        assert not (workspace_path / "TOOLS.md").exists()


def test_bootstrap_strict_mode_keeps_security_templates():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        workspace_path = Path(tmpdir) / "workspace"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_EXEC_STRICT_MODE"] = "true"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert (workspace_path / "SOUL.md").exists()
        assert (workspace_path / "AGENTS.md").exists()
        assert (workspace_path / "MEMORY.md").exists()
        assert (workspace_path / "USER.MD").exists()
        assert (workspace_path / "TOOLS.md").exists()


def test_bootstrap_relaxed_mode_cleans_legacy_builtin_security_templates():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir(parents=True, exist_ok=True)
        (workspace_path / "SOUL.md").write_text("security soul\n")
        (workspace_path / "AGENTS.md").write_text("security agents\n")
        (workspace_path / "MEMORY.md").write_text("persistent memory\n")
        (workspace_path / "USER.MD").write_text("user preferences\n")
        (workspace_path / "TOOLS.md").write_text("tool notes\n")
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
        assert not (workspace_path / "SOUL.md").exists()
        assert not (workspace_path / "AGENTS.md").exists()
        assert not (workspace_path / "MEMORY.md").exists()
        assert not (workspace_path / "USER.MD").exists()
        assert not (workspace_path / "TOOLS.md").exists()


def test_bootstrap_relaxed_mode_preserves_customized_security_templates():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir(parents=True, exist_ok=True)
        soul_path = workspace_path / "SOUL.md"
        agents_path = workspace_path / "AGENTS.md"
        memory_path = workspace_path / "MEMORY.md"
        user_path = workspace_path / "USER.MD"
        tools_path = workspace_path / "TOOLS.md"
        soul_path.write_text("my custom soul\n")
        agents_path.write_text("my custom agents\n")
        memory_path.write_text("my custom memory\n")
        user_path.write_text("my custom user prefs\n")
        tools_path.write_text("my custom tools\n")
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
        assert soul_path.read_text() == "my custom soul\n"
        assert agents_path.read_text() == "my custom agents\n"
        assert memory_path.read_text() == "my custom memory\n"
        assert user_path.read_text() == "my custom user prefs\n"
        assert tools_path.read_text() == "my custom tools\n"


def test_bootstrap_preserves_existing_memory_file():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir(parents=True, exist_ok=True)
        memory_path = workspace_path / "MEMORY.md"
        memory_path.write_text("user customized memory\n")
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
        assert memory_path.read_text() == "user customized memory\n"


def test_bootstrap_strict_mode_restores_allowlist_defaults():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        approvals_path = Path(tmpdir) / "exec-approvals.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_EXEC_STRICT_MODE"] = "true"

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
        assert cfg["tools"]["exec"]["security"] == "allowlist"
        approvals = json.loads(approvals_path.read_text())
        assert approvals["defaults"]["security"] == "allowlist"
        allowlist = approvals["agents"]["main"]["allowlist"]
        patterns = {entry["pattern"] for entry in allowlist}
        command_names = {Path(pattern).name for pattern in patterns}
        assert str(Path(tmpdir) / "safe-bin" / "bash-safe") in patterns
        assert "curl" in command_names
        assert "jq" in command_names
        assert "openclaw" in command_names
        assert "agent-browser" in command_names
        assert "yt-dlp" not in command_names
        assert "gh" not in command_names
        assert "xreach" not in command_names


def test_multi_search_skill_avoids_curl_head_broken_pipe_pattern():
    skill_path = (
        REPO_ROOT
        / "deploy"
        / "openclaw"
        / "preset-skills"
        / "multi-search-engine"
        / "SKILL.md"
    )

    content = skill_path.read_text()

    assert "curl -sS \"https://www.baidu.com/s?wd=QUERY\" | head -200" not in content


def test_multi_search_skill_prefers_cn_bing_and_builtin_browser_first():
    skill_path = (
        REPO_ROOT
        / "deploy"
        / "openclaw"
        / "preset-skills"
        / "multi-search-engine"
        / "SKILL.md"
    )

    content = skill_path.read_text()

    assert "built-in fetch tool" not in content
    assert "start with Bing CN / Sogou / 360 before trying Baidu" in content
    assert "prefer the built-in `browser` tool first in this runtime" in content
    assert "agent-browser open \"https://cn.bing.com/search?q=QUERY&ensearch=0\"" in content
    assert "browser navigate https://www.baidu.com/s?wd=QUERY" not in content
    assert "curl -sS \"https://cn.bing.com/search?q=QUERY\" | head -200" not in content
    assert "Failure writing output to destination" in content


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


def test_bootstrap_keeps_model_api_key_in_gateway_process_env_for_deferred_auth_paths():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        captured_env_path = Path(tmpdir) / "gateway.env"
        fake_bin_dir = Path(tmpdir) / "bin"
        fake_node_path = fake_bin_dir / "node"
        real_node_path = subprocess.run(
            ["bash", "-lc", "command -v node"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        fake_bin_dir.mkdir()
        fake_node_path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "openclaw.mjs" ] && [ "$2" = "gateway" ] && [ "$3" = "run" ]; then\n'
            '  printenv | sort > "${BOOTSTRAP_CAPTURE_ENV_PATH}"\n'
            "  exit 0\n"
            "fi\n"
            f'exec "{real_node_path}" "$@"\n'
        )
        fake_node_path.chmod(0o755)

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["BOOTSTRAP_CAPTURE_ENV_PATH"] = str(captured_env_path)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"
        env["OPENCLAW_RUNTIME_PROXY_ENABLED"] = "0"
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
        env.pop("OPENCLAW_BOOTSTRAP_ONLY", None)

        process = subprocess.Popen(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        deadline = time.monotonic() + 5
        while not captured_env_path.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.05)

        assert captured_env_path.exists(), process.stderr.read() or process.stdout.read()

        process.terminate()
        process.communicate(timeout=5)

        captured_env = captured_env_path.read_text()
        assert "OPENCLAW_MODEL_API_KEY=dummy-secret-value" in captured_env
        assert "OPENAI_API_KEY=" not in captured_env
        assert "OPENCLAW_INTERNAL_TRUSTED_PROXY_USER=openclaw-backend" in captured_env
        assert "OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER=x-forwarded-user" in captured_env
        assert "CLAWHUB_SITE=https://cn.clawhub-mirror.com" in captured_env
        assert "CLAWHUB_REGISTRY=https://cn.clawhub-mirror.com" in captured_env
        assert "NPM_CONFIG_REGISTRY=https://registry.npmmirror.com" in captured_env
        assert "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple" in captured_env
        assert "PIP_TRUSTED_HOST=mirrors.aliyun.com" in captured_env
        assert "UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple" in captured_env
        assert "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright" in captured_env
        assert "PUPPETEER_DOWNLOAD_BASE_URL=https://npmmirror.com/mirrors/chrome-for-testing" in captured_env


def test_bootstrap_does_not_relaunch_gateway_after_upstream_handoff_restart():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        gateway_count_path = Path(tmpdir) / "gateway-count.txt"
        successor_pid_path = Path(tmpdir) / "gateway-successor.pid"
        fake_bin_dir = Path(tmpdir) / "bin"
        fake_node_path = fake_bin_dir / "node"
        real_node_path = subprocess.run(
            ["bash", "-lc", "command -v node"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        fake_bin_dir.mkdir()
        fake_node_path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "openclaw.mjs" ] && [ "$2" = "gateway" ] && [ "$3" = "run" ]; then\n'
            '  count=0\n'
            '  if [ -f "${BOOTSTRAP_GATEWAY_COUNT_PATH}" ]; then\n'
            '    count="$(cat "${BOOTSTRAP_GATEWAY_COUNT_PATH}")"\n'
            "  fi\n"
            '  count=$((count + 1))\n'
            '  printf "%s\\n" "${count}" > "${BOOTSTRAP_GATEWAY_COUNT_PATH}"\n'
            '  if [ "${count}" -eq 1 ]; then\n'
            '    python3 -m http.server "${OPENCLAW_GATEWAY_PORT}" --bind 127.0.0.1 >/dev/null 2>&1 &\n'
            '    printf "%s\\n" "$!" > "${BOOTSTRAP_SUCCESSOR_PID_PATH}"\n'
            "    exit 0\n"
            "  fi\n"
            "  exit 97\n"
            "fi\n"
            f'exec "{real_node_path}" "$@"\n'
        )
        fake_node_path.chmod(0o755)

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["BOOTSTRAP_GATEWAY_COUNT_PATH"] = str(gateway_count_path)
        env["BOOTSTRAP_SUCCESSOR_PID_PATH"] = str(successor_pid_path)
        env["OPENCLAW_GATEWAY_PORT"] = "18080"
        env["OPENCLAW_GATEWAY_LOCAL_RESTART_MAX"] = "0"
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
        env.pop("OPENCLAW_BOOTSTRAP_ONLY", None)

        process = subprocess.Popen(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            deadline = time.monotonic() + 5
            while (
                not gateway_count_path.exists() or not successor_pid_path.exists()
            ) and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.05)

            assert gateway_count_path.exists(), process.stderr.read() or process.stdout.read()
            assert successor_pid_path.exists(), process.stderr.read() or process.stdout.read()

            time.sleep(1.0)
            assert process.poll() is None, process.stderr.read() or process.stdout.read()
            assert gateway_count_path.read_text().strip() == "1"
        finally:
            if successor_pid_path.exists():
                successor_pid = successor_pid_path.read_text().strip()
                if successor_pid:
                    subprocess.run(
                        ["kill", successor_pid],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def test_bootstrap_runtime_proxy_moves_gateway_to_internal_port():
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert 'RUNTIME_PROXY_ENABLED="${OPENCLAW_RUNTIME_PROXY_ENABLED:-true}"' in bootstrap
    assert 'GATEWAY_INTERNAL_PORT="${OPENCLAW_GATEWAY_INTERNAL_PORT:-18080}"' in bootstrap
    assert 'GATEWAY_LISTENER_PORT="${GATEWAY_INTERNAL_PORT:-18080}"' in bootstrap
    assert 'OPENCLAW_GATEWAY_PROXY_BASE_URL="http://127.0.0.1:${GATEWAY_LISTENER_PORT}"' in bootstrap
    assert "uvicorn openclaw_runtime_proxy_app:app" in bootstrap
    assert '--port "${GATEWAY_PORT}"' in bootstrap
    assert 'node openclaw.mjs gateway run --allow-unconfigured --bind "${BIND_MODE}" --port "${GATEWAY_LISTENER_PORT}"' in bootstrap


def test_bootstrap_writes_domestic_runtime_defaults_to_env_file():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
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
        runtime_env = (Path(tmpdir) / ".env").read_text()
        assert "CLAWHUB_SITE=https://cn.clawhub-mirror.com" in runtime_env
        assert "CLAWHUB_REGISTRY=https://cn.clawhub-mirror.com" in runtime_env
        assert "NPM_CONFIG_REGISTRY=https://registry.npmmirror.com" in runtime_env
        assert "npm_config_registry=https://registry.npmmirror.com" in runtime_env
        assert "YARN_NPM_REGISTRY_SERVER=https://registry.npmmirror.com" in runtime_env
        assert "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple" in runtime_env
        assert "PIP_TRUSTED_HOST=mirrors.aliyun.com" in runtime_env
        assert "UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple" in runtime_env
        assert "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright" in runtime_env
        assert "PUPPETEER_DOWNLOAD_BASE_URL=https://npmmirror.com/mirrors/chrome-for-testing" in runtime_env


def test_agent_browser_skill_prefers_domestic_examples():
    skill_path = (
        REPO_ROOT
        / "deploy"
        / "openclaw"
        / "preset-skills"
        / "agent-browser-clawdbot"
        / "SKILL.md"
    )

    content = skill_path.read_text()

    assert "agent-browser open https://www.google.com" not in content
    assert "agent-browser open https://www.baidu.com" not in content
    assert "agent-browser open https://cn.bing.com/search?q=AI+agents&ensearch=0" in content
    assert "https://www.bing.com/news/search?q=AI&mkt=zh-CN" in content
    assert "This image already bundles `agent-browser`" in content
    assert "NPM_CONFIG_REGISTRY" in content
    assert "PLAYWRIGHT_DOWNLOAD_HOST" in content
    assert "Built-in `browser` is enabled by default in this image." in content
    assert "Use `web-safe search` / `web-safe read` only when a cheap read-only fallback is enough" in content
    assert "The task is small enough that a one-off interactive browser session is simpler" not in content


def test_bootstrap_does_not_auto_register_exa_defaults():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        raw_bin_dir = Path(tmpdir) / "bin"
        capture_path = Path(tmpdir) / "mcporter.log"
        raw_bin_dir.mkdir(parents=True, exist_ok=True)
        mcporter_path = raw_bin_dir / "mcporter"
        mcporter_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"config\" ] && [ \"$2\" = \"get\" ]; then\n"
            "  exit 1\n"
            "fi\n"
            "printf '%s\\n' \"$*\" >> \"$MCPORTER_CAPTURE_PATH\"\n"
            "exit 0\n"
        )
        mcporter_path.chmod(0o755)

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["MCPORTER_CAPTURE_PATH"] = str(capture_path)

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert not capture_path.exists()


def test_bootstrap_seeds_and_auto_enables_bundled_weixin_plugin():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-weixin"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

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
        assert (Path(tmpdir) / "extensions" / "openclaw-weixin" / "manifest.json").exists()
        assert cfg["plugins"]["entries"]["openclaw-weixin"]["enabled"] is True


def test_bootstrap_does_not_seed_deferred_mem0_plugin_by_default():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-mem0"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-mem0"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert not (Path(tmpdir) / "extensions" / "openclaw-mem0").exists()
        cfg = json.loads(config_path.read_text())
        assert "openclaw-mem0" not in (cfg.get("plugins", {}).get("entries", {}) or {})


def test_bootstrap_preserves_user_managed_weixin_plugin_in_existing_extension_dir():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"2.0.0"}\n'
        )
        (default_extensions_dir / "README.md").write_text("bundled-v2\n")

        existing_extension_dir = Path(tmpdir) / "extensions" / "openclaw-weixin"
        existing_extension_dir.mkdir(parents=True, exist_ok=True)
        (existing_extension_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"1.0.0"}\n'
        )
        (existing_extension_dir / "stale.txt").write_text("old-plugin-layout\n")

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert (existing_extension_dir / "manifest.json").read_text() == (
            '{"name":"openclaw-weixin","version":"1.0.0"}\n'
        )
        assert not (existing_extension_dir / "README.md").exists()
        assert (existing_extension_dir / "stale.txt").read_text() == "old-plugin-layout\n"


def test_bootstrap_upgrades_previously_synced_weixin_plugin_when_bundle_changes():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        (default_extensions_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"1.0.0"}\n'
        )
        (default_extensions_dir / "README.md").write_text("bundled-v1\n")

        first = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert first.returncode == 0, first.stderr or first.stdout

        (default_extensions_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"2.0.0"}\n'
        )
        (default_extensions_dir / "README.md").write_text("bundled-v2\n")

        second = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert second.returncode == 0, second.stderr or second.stdout

        existing_extension_dir = Path(tmpdir) / "extensions" / "openclaw-weixin"
        assert (existing_extension_dir / "manifest.json").read_text() == (
            '{"name":"openclaw-weixin","version":"2.0.0"}\n'
        )
        assert (existing_extension_dir / "README.md").read_text() == "bundled-v2\n"


def test_bootstrap_preserves_user_modified_weixin_plugin_after_initial_seed():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        (default_extensions_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"1.0.0"}\n'
        )

        first = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert first.returncode == 0, first.stderr or first.stdout

        existing_extension_dir = Path(tmpdir) / "extensions" / "openclaw-weixin"
        (existing_extension_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"9.9.9-user"}\n'
        )
        (existing_extension_dir / "USER.md").write_text("custom-user-plugin\n")
        (default_extensions_dir / "manifest.json").write_text(
            '{"name":"openclaw-weixin","version":"2.0.0"}\n'
        )
        (default_extensions_dir / "README.md").write_text("bundled-v2\n")

        second = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert second.returncode == 0, second.stderr or second.stdout

        assert (existing_extension_dir / "manifest.json").read_text() == (
            '{"name":"openclaw-weixin","version":"9.9.9-user"}\n'
        )
        assert (existing_extension_dir / "USER.md").read_text() == "custom-user-plugin\n"
        assert not (existing_extension_dir / "README.md").exists()


def test_bootstrap_preserves_existing_weixin_plugin_disablement():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-weixin"}\n')
        config_path.write_text(
            json.dumps(
                {
                    "plugins": {
                        "entries": {
                            "openclaw-weixin": {
                                "enabled": False,
                            }
                        }
                    }
                }
            )
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

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
        assert (Path(tmpdir) / "extensions" / "openclaw-weixin" / "manifest.json").exists()
        assert cfg["plugins"]["entries"]["openclaw-weixin"]["enabled"] is False


def test_bootstrap_auto_enables_bundled_lark_plugin():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-lark"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-lark"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

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
        assert (Path(tmpdir) / "extensions" / "openclaw-lark" / "manifest.json").exists()
        assert cfg["plugins"]["entries"]["openclaw-lark"]["enabled"] is True


def test_bootstrap_configures_wps_xiezuo_channel_from_channel_bootstrap_json():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "wps-xiezuo"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"wps-xiezuo"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")
        env["OPENCLAW_CHANNEL_BOOTSTRAP_JSON"] = json.dumps(
            {
                "wps-xiezuo": {
                    "appId": "app-demo",
                    "appSecret": "secret-demo",
                }
            }
        )

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
        assert (Path(tmpdir) / "extensions" / "wps-xiezuo" / "manifest.json").exists()
        assert cfg["plugins"]["entries"]["wps-xiezuo"]["enabled"] is True
        assert "wps-xiezuo" in cfg["plugins"]["allow"]
        channel = cfg["channels"]["wps-xiezuo"]
        assert channel["enabled"] is True
        assert channel["appId"] == "app-demo"
        assert channel["appSecret"] == "secret-demo"
        assert channel["baseUrl"] == "https://openapi.wps.cn"
        assert channel["sdk"] == {"enabled": True, "logLevel": "info"}
        assert channel["dmPolicy"] == "open"
        assert channel["allowFrom"] == ["*"]
        assert channel["groupPolicy"] == "open"
        assert channel["instantAck"]["text"] == "内容处理中，请稍候..."
        assert channel["mcp"]["enabled"] is True
        assert channel["mcp"]["mode"] == "app"
        assert "toolAllowlist" not in channel["mcp"]
        assert "accounts" not in channel
        assert "defaultAccountId" not in channel
        assert {"type": "route", "agentId": "main", "match": {"channel": "wps-xiezuo"}} in cfg["bindings"]


def test_bootstrap_allows_wps_xiezuo_channel_without_complete_credentials():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "wps-xiezuo"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"wps-xiezuo"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")
        env["OPENCLAW_CHANNEL_BOOTSTRAP_JSON"] = json.dumps(
            {
                "wps-xiezuo": {
                    "appId": "app-demo",
                }
            }
        )

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
        channel = cfg["channels"]["wps-xiezuo"]
        assert channel["enabled"] is True
        assert channel["appId"] == "app-demo"
        assert channel["appSecret"] == ""
        assert channel["baseUrl"] == "https://openapi.wps.cn"
        assert channel["dmPolicy"] == "open"
        assert channel["allowFrom"] == ["*"]
        assert cfg["plugins"]["entries"]["wps-xiezuo"]["enabled"] is True
        assert "wps-xiezuo" in cfg["plugins"]["allow"]


def test_bootstrap_preserves_explicit_wps_xiezuo_mcp_tool_allowlist():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "wps-xiezuo"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"wps-xiezuo"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")
        env["OPENCLAW_CHANNEL_BOOTSTRAP_JSON"] = json.dumps(
            {
                "wps-xiezuo": {
                    "appId": "app-demo",
                    "appSecret": "secret-demo",
                    "mcp": {
                        "enabled": True,
                        "mode": "app",
                        "toolAllowlist": ["wps_message_send"],
                    },
                }
            }
        )

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
        assert cfg["channels"]["wps-xiezuo"]["mcp"]["toolAllowlist"] == ["wps_message_send"]


def test_bootstrap_rewrites_stale_wps_xiezuo_accounts_to_flat_channel_config():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "channels": {
                        "wps-xiezuo": {
                            "accounts": {
                                "default": {
                                    "appId": "app-stale",
                                }
                            }
                        }
                    }
                }
            )
        )
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "wps-xiezuo"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"wps-xiezuo"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")
        env["OPENCLAW_CHANNEL_BOOTSTRAP_JSON"] = json.dumps(
            {
                "wps-xiezuo": {
                    "appId": "app-demo",
                    "appSecret": "secret-demo",
                }
            }
        )

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
        channel = cfg["channels"]["wps-xiezuo"]
        assert channel["appId"] == "app-demo"
        assert channel["appSecret"] == "secret-demo"
        assert "accounts" not in channel
        assert "defaultAccountId" not in channel


def test_bootstrap_configures_feishu_channel_from_channel_bootstrap_json():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-lark"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-lark"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")
        env["OPENCLAW_CHANNEL_BOOTSTRAP_JSON"] = json.dumps(
            {
                "feishu": {
                    "appId": "cli-app-id",
                    "appSecret": "cli-app-secret",
                    "domain": "lark",
                }
            }
        )

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
        assert cfg["plugins"]["entries"]["openclaw-lark"]["enabled"] is True
        assert cfg["channels"]["feishu"]["enabled"] is True
        assert cfg["channels"]["feishu"]["appId"] == "cli-app-id"
        assert cfg["channels"]["feishu"]["appSecret"] == "cli-app-secret"
        assert cfg["channels"]["feishu"]["domain"] == "lark"
        assert cfg["channels"]["feishu"]["connectionMode"] == "websocket"
        assert cfg["channels"]["feishu"]["requireMention"] is True
        assert cfg["channels"]["feishu"]["dmPolicy"] == "pairing"
        assert cfg["channels"]["feishu"]["groupPolicy"] == "open"


def test_bootstrap_keeps_feishu_open_dm_policy_valid_when_existing_allow_from_is_specific():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "channels": {
                        "feishu": {
                            "enabled": True,
                            "appId": "cli-app-id",
                            "appSecret": "cli-app-secret",
                            "dmPolicy": "open",
                            "allowFrom": ["ou_demo_1", "ou_demo_2"],
                        }
                    }
                }
            )
        )
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-lark"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-lark"}\n')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

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
        assert cfg["channels"]["feishu"]["dmPolicy"] == "open"
        assert cfg["channels"]["feishu"]["allowFrom"] == ["ou_demo_1", "ou_demo_2", "*"]


def test_bootstrap_patches_bundled_weixin_gateway_login_methods_before_sync():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        plugin_root = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        plugin_dir = plugin_root / "src"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        _write_weixin_plugin_package_json(plugin_root)
        (plugin_dir / "channel.ts").write_text(
            "export const weixinPlugin = {\n"
            "  status: {\n"
            "    defaultRuntime: {},\n"
            "  },\n"
            "};\n"
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        bundled_source = (Path(tmpdir) / "default-extensions" / "openclaw-weixin" / "src" / "channel.ts").read_text()
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in bundled_source
        patched_source = (Path(tmpdir) / "extensions" / "openclaw-weixin" / "src" / "channel.ts").read_text()
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in patched_source


def test_bootstrap_patches_latest_weixin_gateway_login_methods_without_version_skip():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        plugin_root = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        plugin_src_dir = plugin_root / "src"

        plugin_src_dir.mkdir(parents=True, exist_ok=True)
        _write_weixin_plugin_package_json(plugin_root, version="2.1.7")
        (plugin_src_dir / "channel.ts").write_text(
            'import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";\n'
            'import { normalizeAccountId } from "openclaw/plugin-sdk/account-id";\n'
            'import { resolvePreferredOpenClawTmpDir } from "openclaw/plugin-sdk/infra-runtime";\n'
            'export const weixinPlugin = {\n'
            '  status: {},\n'
            '};\n'
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        patched_source = (Path(tmpdir) / "extensions" / "openclaw-weixin" / "src" / "channel.ts").read_text()
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in patched_source
        assert "skipped bundled channel plugin compat patch" not in result.stderr


def test_bootstrap_only_adds_gateway_login_methods_for_target_weixin_version():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        plugin_root = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        (plugin_root / "src").mkdir(parents=True, exist_ok=True)

        _write_weixin_plugin_package_json(plugin_root)
        (plugin_root / "index.ts").write_text(
            'import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";\n'
            'import { buildChannelConfigSchema } from "openclaw/plugin-sdk/channel-config-schema";\n'
        )
        (plugin_root / "src" / "channel.ts").write_text(
            'import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";\n'
            'import { normalizeAccountId } from "openclaw/plugin-sdk/account-id";\n'
            'import { resolvePreferredOpenClawTmpDir } from "openclaw/plugin-sdk/infra-runtime";\n'
            'export const weixinPlugin = {\n'
            '  status: {},\n'
            '};\n'
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        synced_root = Path(tmpdir) / "extensions" / "openclaw-weixin"
        assert 'openclaw/plugin-sdk/plugin-entry' in (synced_root / "index.ts").read_text()
        patched_channel = (synced_root / "src" / "channel.ts").read_text()
        assert 'openclaw/plugin-sdk/infra-runtime' in patched_channel
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in patched_channel
        assert not (synced_root / "node_modules" / "openclaw").exists()


def test_bootstrap_patches_weixin_remote_login_patch_for_newer_official_version():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        plugin_root = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        plugin_src_dir = plugin_root / "src"

        plugin_src_dir.mkdir(parents=True, exist_ok=True)

        _write_weixin_plugin_package_json(plugin_root, version="2.1.8")
        (plugin_src_dir / "channel.ts").write_text(
            'import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";\n'
            'import { normalizeAccountId } from "openclaw/plugin-sdk/account-id";\n'
            'import { resolvePreferredOpenClawTmpDir } from "openclaw/plugin-sdk/infra-runtime";\n'
            'export const weixinPlugin = {\n'
            '  status: {},\n'
            '};\n'
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        bundled_source = (plugin_root / "src" / "channel.ts").read_text()
        synced_source = (Path(tmpdir) / "extensions" / "openclaw-weixin" / "src" / "channel.ts").read_text()
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in bundled_source
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in synced_source
        assert "skipped bundled channel plugin compat patch" not in result.stderr


def test_bootstrap_skips_weixin_remote_login_patch_for_older_official_version():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        plugin_root = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        plugin_src_dir = plugin_root / "src"

        plugin_src_dir.mkdir(parents=True, exist_ok=True)

        _write_weixin_plugin_package_json(plugin_root, version="2.0.2")
        (plugin_src_dir / "channel.ts").write_text(
            'import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";\n'
            'import { normalizeAccountId } from "openclaw/plugin-sdk/account-id";\n'
            'import { resolvePreferredOpenClawTmpDir } from "openclaw/plugin-sdk/infra-runtime";\n'
            'export const weixinPlugin = {\n'
            '  status: {},\n'
            '};\n'
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        bundled_source = (plugin_root / "src" / "channel.ts").read_text()
        synced_source = (Path(tmpdir) / "extensions" / "openclaw-weixin" / "src" / "channel.ts").read_text()
        assert 'openclaw/plugin-sdk/infra-runtime' in bundled_source
        assert 'openclaw/plugin-sdk/infra-runtime' in synced_source
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' not in bundled_source
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' not in synced_source
        assert "skipped bundled channel plugin compat patch" in result.stderr


def test_bootstrap_does_not_runtime_patch_user_managed_weixin_plugin():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        bundled_plugin_root = Path(tmpdir) / "default-extensions" / "openclaw-weixin"
        existing_plugin_root = Path(tmpdir) / "extensions" / "openclaw-weixin"
        (bundled_plugin_root / "src").mkdir(parents=True, exist_ok=True)
        (existing_plugin_root / "src").mkdir(parents=True, exist_ok=True)

        _write_weixin_plugin_package_json(bundled_plugin_root, version="2.1.7")
        _write_weixin_plugin_package_json(existing_plugin_root, version="9.9.9-user")
        (bundled_plugin_root / "src" / "channel.ts").write_text(
            'export const weixinPlugin = {\n'
            '  status: {},\n'
            '};\n'
        )
        original_user_source = (
            'import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";\n'
            'import { normalizeAccountId } from "openclaw/plugin-sdk/account-id";\n'
            'import { resolvePreferredOpenClawTmpDir } from "openclaw/plugin-sdk/infra-runtime";\n'
            'export const weixinPlugin = {\n'
            '  status: {},\n'
            '};\n'
        )
        (existing_plugin_root / "src" / "channel.ts").write_text(original_user_source)

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert (existing_plugin_root / "src" / "channel.ts").read_text() == original_user_source
        assert "preserved user-managed extension openclaw-weixin" in result.stderr


def test_bootstrap_runtime_patches_existing_official_weixin_216_plugin():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        existing_plugin_root = Path(tmpdir) / "extensions" / "openclaw-weixin"

        (existing_plugin_root / "src").mkdir(parents=True, exist_ok=True)

        _write_weixin_plugin_package_json(existing_plugin_root, version="2.1.7")
        (existing_plugin_root / "index.ts").write_text(
            'import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";\n'
        )
        (existing_plugin_root / "src" / "channel.ts").write_text(
            'import type { ChannelPlugin, OpenClawConfig } from "openclaw/plugin-sdk/core";\n'
            'import { normalizeAccountId } from "openclaw/plugin-sdk/account-id";\n'
            'import { resolvePreferredOpenClawTmpDir } from "openclaw/plugin-sdk/infra-runtime";\n'
            'export const weixinPlugin = {\n'
            '  status: {\n'
            '    defaultRuntime: {},\n'
            '  },\n'
            '};\n'
        )

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert 'openclaw/plugin-sdk/plugin-entry' in (existing_plugin_root / "index.ts").read_text()
        patched_channel = (existing_plugin_root / "src" / "channel.ts").read_text()
        assert 'openclaw/plugin-sdk/infra-runtime' in patched_channel
        assert 'gatewayMethods: ["web.login.start", "web.login.wait"],' in patched_channel
        assert not (existing_plugin_root / "node_modules" / "openclaw").exists()


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
            "clawhub-store",
            "agent-browser-clawdbot",
            "self-improving-agent",
            "kdocs",
            "agent-reach",
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
            "agent-browser-clawdbot",
            "clawhub-store",
            "kdocs",
        ]
        cfg = json.loads(config_path.read_text())
        assert cfg["skills"]["allowBundled"] == [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
            "wps365-skill",
        ]


def test_bootstrap_strict_mode_keeps_tuanziguardianclaw_preset_skill():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        preset_skills_dir = Path(tmpdir) / "preset-skills"
        for skill_name in [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
            "self-improving-agent",
            "tuanziguardianclaw",
        ]:
            skill_dir = preset_skills_dir / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(f"{skill_name}\n")

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_PRESET_SKILLS_DIR"] = str(preset_skills_dir)
        env["OPENCLAW_EXEC_STRICT_MODE"] = "true"

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
            "agent-browser-clawdbot",
            "clawhub-store",
            "kdocs",
            "self-improving-agent",
            "tuanziguardianclaw",
        ]
        cfg = json.loads(config_path.read_text())
        assert cfg["skills"]["allowBundled"] == [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
            "wps365-skill",
            "self-improving-agent",
            "tuanziguardianclaw",
        ]


def test_bootstrap_overrides_stale_bundled_skill_allowlist_from_existing_config():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "skills": {
                        "allowBundled": [
                            "clawhub-store",
                            "tavily-search",
                            "agent-reach",
                        ]
                    }
                }
            )
        )
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
        assert cfg["skills"]["allowBundled"] == [
            "clawhub-store",
            "agent-browser-clawdbot",
            "kdocs",
            "wps365-skill",
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
        server_bundle = dist_dir / "server.impl-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);\n'
            '}\n'
            'if (isLoopbackAddress(remoteAddr)) return { reason: "trusted_proxy_loopback_source" };\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\treturn true;\n'
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
        server_bundle.write_text(
            'function createGatewayHttpServer(opts) {\n'
            '\tconst { canvasHost, clients, controlUiEnabled, controlUiBasePath, controlUiRoot, openAiChatCompletionsEnabled, openAiChatCompletionsConfig, openResponsesEnabled, openResponsesConfig, strictTransportSecurityHeader, handleHooksRequest, handlePluginRequest, shouldEnforcePluginGatewayAuth, resolvedAuth, rateLimiter, getReadiness } = opts;\n'
            '\tconst getResolvedAuth = opts.getResolvedAuth ?? (() => resolvedAuth);\n'
            '\tconst openAiCompatEnabled = openAiChatCompletionsEnabled || openResponsesEnabled;\n'
            '\tasync function handleRequest(req, res) {\n'
            '\t\tconst requestPath = new URL(req.url ?? "/", "http://localhost").pathname;\n'
            '\t\tconst requestStages = [{\n'
            '\t\t\tname: "hooks",\n'
            '\t\t\trun: () => handleHooksRequest(req, res)\n'
            '\t\t}];\n'
            '\t\tif (controlUiEnabled) {\n'
            '\t\t\trequestStages.push({\n'
            '\t\t\t\tname: "control-ui-http",\n'
            '\t\t\t\trun: async () => (await getControlUiModule()).handleControlUiHttpRequest(req, res, {\n'
            '\t\t\t\t\tbasePath: controlUiBasePath,\n'
            '\t\t\t\t\tconfig: configSnapshot,\n'
            '\t\t\t\t\tagentId: resolveAssistantIdentity({ cfg: configSnapshot }).agentId,\n'
            '\t\t\t\t\troot: controlUiRoot\n'
            '\t\t\t\t})\n'
            '\t\t\t});\n'
            '\t\t}\n'
            '\t}\n'
            '}\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

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
        assert 'const usesDeviceTokenAuth = params.authMethod === "device-token";' in gateway_source
        assert 'usesLoopbackTrustedProxyAuth || params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth' in gateway_source
        assert 'const internalLoopbackUserHeader = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER || process.env.OPENCLAW_TRUSTED_PROXY_USER_HEADER || "x-forwarded-user").trim().toLowerCase();' in gateway_source
        assert 'const loopbackUser = headerValue(req.headers[internalLoopbackUserHeader || "x-forwarded-user"]);' in gateway_source
        assert 'const forwardedLoopbackChain = String(headerValue(req.headers["x-forwarded-for"]) || "").split(",").map((value) => value.trim()).filter(Boolean);' in gateway_source
        assert 'const trustedProxyAddressCheck = typeof isTrustedProxyAddress === "function" ? isTrustedProxyAddress : typeof isTrustedProxyAddress$1 === "function" ? isTrustedProxyAddress$1 : null;' in gateway_source
        assert 'const forwardedLoopbackTrusted = !!trustedProxyAddressCheck && forwardedLoopbackChain.some((addr) => !isLoopbackAddress(addr) && trustedProxyAddressCheck(addr, trustedProxies));' in gateway_source
        assert 'if (!forwardedLoopbackTrusted && (!internalLoopbackUser || !loopbackUser || loopbackUser.trim() !== internalLoopbackUser)) return { reason: "trusted_proxy_loopback_source" };' in gateway_source
        assert 'function shouldAttachDeviceIdentityForGatewayCall(params) {' in gateway_source
        assert '].includes(parsed.hostname)) return false;' in gateway_source
        assert '}) ? loadOrCreateDeviceIdentity() : null,' in gateway_source
        assert 'const parsed = new URL(params.urlOverride);' in gateway_source
        assert 'if (["127.0.0.1", "::1", "localhost"].includes(parsed.hostname)) return;' in gateway_source
        assert 'const keepUnboundScopes = !device && decision.kind === "allow" && authMethod === "trusted-proxy" && !hasBrowserOriginHeader;' in gateway_source
        server_source = server_bundle.read_text()
        assert 'async function handleWorkspaceFilesProxyRequest(req, res) {' not in server_source
        assert 'name: "workspace-files-proxy"' not in server_source
        assert 'requestUrl.pathname.startsWith("/_ksadk/workspace/v1/")' not in server_source
        assert 'const targetUrl = new URL(`${requestUrl.pathname}${requestUrl.search}`' not in server_source
        assert 'this.ws.addEventListener(`open`,()=>{this.lastSeq=null,this.queueConnect()})' in control_ui_bundle.read_text()


def test_bootstrap_patches_allow_loopback_runtime_for_forwarded_trusted_proxy_chain():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        gateway_bundle = dist_dir / "gateway-cli-test.js"
        server_bundle = dist_dir / "server.impl-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);\n'
            '}\n'
            'function authorizeTrustedProxy(params) {\n'
            '\tconst { req, trustedProxies, trustedProxyConfig } = params;\n'
            '\tconst remoteAddr = req.socket?.remoteAddress;\n'
            '\tif (isLoopbackAddress(remoteAddr) && trustedProxyConfig.allowLoopback !== true) return { reason: "trusted_proxy_loopback_source" };\n'
            '\treturn { user: headerValue(req.headers[trustedProxyConfig.userHeader.toLowerCase()]).trim() };\n'
            '}\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\treturn true;\n'
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
        server_bundle.write_text(
            'function createGatewayHttpServer(opts) {\n'
            '\tconst { canvasHost, clients, controlUiEnabled, controlUiBasePath, controlUiRoot, openAiChatCompletionsEnabled, openAiChatCompletionsConfig, openResponsesEnabled, openResponsesConfig, strictTransportSecurityHeader, handleHooksRequest, handlePluginRequest, shouldEnforcePluginGatewayAuth, resolvedAuth, rateLimiter, getReadiness } = opts;\n'
            '\tconst getResolvedAuth = opts.getResolvedAuth ?? (() => resolvedAuth);\n'
            '\tconst openAiCompatEnabled = openAiChatCompletionsEnabled || openResponsesEnabled;\n'
            '\tasync function handleRequest(req, res) {\n'
            '\t\tconst requestPath = new URL(req.url ?? "/", "http://localhost").pathname;\n'
            '\t\tconst requestStages = [{\n'
            '\t\t\tname: "hooks",\n'
            '\t\t\trun: () => handleHooksRequest(req, res)\n'
            '\t\t}];\n'
            '\t\tif (controlUiEnabled) {\n'
            '\t\t\trequestStages.push({\n'
            '\t\t\t\tname: "control-ui-http",\n'
            '\t\t\t\trun: async () => (await getControlUiModule()).handleControlUiHttpRequest(req, res, {\n'
            '\t\t\t\t\tbasePath: controlUiBasePath,\n'
            '\t\t\t\t\tconfig: configSnapshot,\n'
            '\t\t\t\t\tagentId: resolveAssistantIdentity({ cfg: configSnapshot }).agentId,\n'
            '\t\t\t\t\troot: controlUiRoot\n'
            '\t\t\t\t})\n'
            '\t\t\t});\n'
            '\t\t}\n'
            '\t}\n'
            '}\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        gateway_source = gateway_bundle.read_text()
        assert 'if (isLoopbackAddress(remoteAddr) && trustedProxyConfig.allowLoopback !== true) {' in gateway_source
        assert 'const forwardedLoopbackChain = String(headerValue(req.headers["x-forwarded-for"]) || "").split(",").map((value) => value.trim()).filter(Boolean);' in gateway_source
        assert 'const trustedProxyAddressCheck = typeof isTrustedProxyAddress === "function" ? isTrustedProxyAddress : typeof isTrustedProxyAddress$1 === "function" ? isTrustedProxyAddress$1 : null;' in gateway_source
        assert 'const forwardedLoopbackTrusted = !!trustedProxyAddressCheck && forwardedLoopbackChain.some((addr) => !isLoopbackAddress(addr) && trustedProxyAddressCheck(addr, trustedProxies));' in gateway_source
        assert 'if (!forwardedLoopbackTrusted && (!internalLoopbackUser || !loopbackUser || loopbackUser.trim() !== internalLoopbackUser)) return { reason: "trusted_proxy_loopback_source" };' in gateway_source


def test_bootstrap_patches_openclaw_2026_5_18_split_auth_and_message_handler_runtime():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        auth_bundle = dist_dir / "auth-test.js"
        message_handler_bundle = dist_dir / "message-handler-test.js"
        gateway_call_bundle = dist_dir / "gateway-call-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        auth_bundle.write_text(
            'function authorizeTrustedProxy(params) {\n'
            '\tconst { req, trustedProxies, trustedProxyConfig } = params;\n'
            '\tif (!req) return { reason: "trusted_proxy_no_request" };\n'
            '\tconst remoteAddr = req.socket?.remoteAddress;\n'
            '\tif (!remoteAddr || !isTrustedProxyAddress(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };\n'
            '\tconst remoteIsLoopback = isLoopbackAddress(remoteAddr);\n'
            '\tif (remoteIsLoopback && trustedProxyConfig.allowLoopback !== true) return { reason: "trusted_proxy_loopback_source" };\n'
            '\treturn { user: headerValue(req.headers[trustedProxyConfig.userHeader.toLowerCase()]).trim() };\n'
            '}\n'
        )
        message_handler_bundle.write_text(
            'function shouldSkipLocalBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tif (!(params.locality === "direct_local" || params.locality === "shared_secret_loopback_local") || params.hasBrowserOriginHeader) return false;\n'
            '\tif (params.authMethod === "none") return true;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth;\n'
            '}\n'
        )
        gateway_call_bundle.write_text(
            'function ensureExplicitGatewayAuth(params) {\n'
            '\tif (!params.urlOverride) return;\n'
            '\tconst explicitToken = params.explicitAuth?.token;\n'
            '}\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        auth_source = auth_bundle.read_text()
        assert 'if (remoteIsLoopback && trustedProxyConfig.allowLoopback !== true) {' in auth_source
        assert 'const forwardedLoopbackChain = String(headerValue(req.headers["x-forwarded-for"]) || "").split(",").map((value) => value.trim()).filter(Boolean);' in auth_source
        assert 'const trustedProxyAddressCheck = typeof isTrustedProxyAddress === "function" ? isTrustedProxyAddress : typeof isTrustedProxyAddress$1 === "function" ? isTrustedProxyAddress$1 : null;' in auth_source
        assert 'const forwardedLoopbackTrusted = !!trustedProxyAddressCheck && forwardedLoopbackChain.some((addr) => !isLoopbackAddress(addr) && trustedProxyAddressCheck(addr, trustedProxies));' in auth_source
        assert 'if (!forwardedLoopbackTrusted && (!internalLoopbackUser || !loopbackUser || loopbackUser.trim() !== internalLoopbackUser)) return { reason: "trusted_proxy_loopback_source" };' in auth_source
        message_handler_source = message_handler_bundle.read_text()
        assert 'const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";' in message_handler_source
        assert 'return usesLoopbackTrustedProxyAuth || params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth;' in message_handler_source


def test_bootstrap_patches_openclaw_2026_5_26_control_ui_trusted_proxy_scopes():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        auth_bundle = dist_dir / "auth-test.js"
        message_handler_bundle = dist_dir / "message-handler-test.js"
        gateway_call_bundle = dist_dir / "gateway-call-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        auth_bundle.write_text(
            'function authorizeTrustedProxy(params) {\n'
            '\tconst { req, trustedProxies, trustedProxyConfig } = params;\n'
            '\tif (!req) return { reason: "trusted_proxy_no_request" };\n'
            '\tconst remoteAddr = req.socket?.remoteAddress;\n'
            '\tif (!remoteAddr || !isTrustedProxyAddress(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };\n'
            '\tconst remoteIsLoopback = isLoopbackAddress(remoteAddr);\n'
            '\tif (remoteIsLoopback && trustedProxyConfig.allowLoopback !== true) return { reason: "trusted_proxy_loopback_source" };\n'
            '\treturn { user: headerValue(req.headers[trustedProxyConfig.userHeader.toLowerCase()]).trim() };\n'
            '}\n'
        )
        message_handler_bundle.write_text(
            'function shouldSkipLocalBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tif (!(params.locality === "direct_local" || params.locality === "shared_secret_loopback_local") || params.hasBrowserOriginHeader) return false;\n'
            '\tif (params.authMethod === "none") return true;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth;\n'
            '}\n'
            'function shouldClearUnboundScopesForMissingDeviceIdentity(params) {\n'
            '\treturn params.decision.kind !== "allow" || !params.controlUiAuthPolicy.allowBypass && !params.preserveInsecureLocalControlUiScopes && (params.authMethod === "token" || params.authMethod === "password" || params.authMethod === "trusted-proxy");\n'
            '}\n'
        )
        gateway_call_bundle.write_text(
            'function ensureExplicitGatewayAuth(params) {\n'
            '\tif (!params.urlOverride) return;\n'
            '\tconst explicitToken = params.explicitAuth?.token;\n'
            '}\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        message_handler_source = message_handler_bundle.read_text()
        assert 'const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";' in message_handler_source
        assert 'return usesLoopbackTrustedProxyAuth || params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth;' in message_handler_source
        assert '!params.trustedProxyAuthOk' in message_handler_source


def test_bootstrap_patches_openclaw_2026_5_26_config_schema_full_response_budget():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        auth_bundle = dist_dir / "auth-test.js"
        message_handler_bundle = dist_dir / "message-handler-test.js"
        gateway_call_bundle = dist_dir / "gateway-call-test.js"
        config_bundle = dist_dir / "config-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        auth_bundle.write_text(
            'function authorizeTrustedProxy(params) {\n'
            '\tconst { req, trustedProxies, trustedProxyConfig } = params;\n'
            '\tif (!req) return { reason: "trusted_proxy_no_request" };\n'
            '\tconst remoteAddr = req.socket?.remoteAddress;\n'
            '\tif (!remoteAddr || !isTrustedProxyAddress(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };\n'
            '\tconst remoteIsLoopback = isLoopbackAddress(remoteAddr);\n'
            '\tif (remoteIsLoopback && trustedProxyConfig.allowLoopback !== true) return { reason: "trusted_proxy_loopback_source" };\n'
            '\treturn { user: headerValue(req.headers[trustedProxyConfig.userHeader.toLowerCase()]).trim() };\n'
            '}\n'
        )
        message_handler_bundle.write_text(
            'function shouldSkipLocalBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tif (!(params.locality === "direct_local" || params.locality === "shared_secret_loopback_local") || params.hasBrowserOriginHeader) return false;\n'
            '\tif (params.authMethod === "none") return true;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth;\n'
            '}\n'
            'function shouldClearUnboundScopesForMissingDeviceIdentity(params) {\n'
            '\treturn params.decision.kind !== "allow" || !params.controlUiAuthPolicy.allowBypass && !params.preserveInsecureLocalControlUiScopes && (params.authMethod === "token" || params.authMethod === "password" || params.authMethod === "trusted-proxy");\n'
            '}\n'
        )
        gateway_call_bundle.write_text(
            'function ensureExplicitGatewayAuth(params) {\n'
            '\tif (!params.urlOverride) return;\n'
            '\tconst explicitToken = params.explicitAuth?.token;\n'
            '}\n'
        )
        config_bundle.write_text(
            'function loadSchemaWithPlugins() {\n'
            '\treturn loadGatewayRuntimeConfigSchema();\n'
            '}\n'
            'const configHandlers = {\n'
            '\t"config.get": async ({ params, respond }) => {\n'
            '\t\trespond(true, redactConfigSnapshot(await readConfigFileSnapshot(), loadSchemaWithPlugins().uiHints), void 0);\n'
            '\t},\n'
            '\t"config.schema": ({ params, respond }) => {\n'
            '\t\tif (!assertValidParams(params, validateConfigSchemaParams, "config.schema", respond)) return;\n'
            '\t\trespond(true, loadSchemaWithPlugins(), void 0);\n'
            '\t},\n'
            '\t"config.schema.lookup": ({ params, respond, context }) => {\n'
            '\t\tconst result = lookupConfigSchema(loadSchemaWithPlugins(), params.path, resolveConfigReloadMetadata);\n'
            '\t\trespond(true, result, void 0);\n'
            '\t}\n'
            '};\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        config_source = config_bundle.read_text()
        assert "compactConfigSchemaResponseForAgentEngineGateway" in config_source
        assert (
            "respond(true, compactConfigSchemaResponseForAgentEngineGateway(loadSchemaWithPlugins()), void 0);"
            in config_source
        )
        assert (
            "lookupConfigSchema(loadSchemaWithPlugins(), params.path, resolveConfigReloadMetadata)"
            in config_source
        )


def test_bootstrap_patches_workspace_proxy_stage_for_upstream_2026_4_26_shape():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        gateway_bundle = dist_dir / "gateway-cli-test.js"
        server_bundle = dist_dir / "server.impl-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);\n'
            '}\n'
            'if (isLoopbackAddress(remoteAddr)) return { reason: "trusted_proxy_loopback_source" };\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\treturn true;\n'
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
        server_bundle.write_text(
            'function createGatewayHttpServer(opts) {\n'
            '\tconst { canvasHost, clients, controlUiEnabled, controlUiBasePath, controlUiRoot, openAiChatCompletionsEnabled, openAiChatCompletionsConfig, openResponsesEnabled, openResponsesConfig, strictTransportSecurityHeader, handleHooksRequest, handlePluginRequest, shouldEnforcePluginGatewayAuth, resolvedAuth, trustedProxies, allowRealIpFallback, rateLimiter, getReadiness } = opts;\n'
            '\tconst getResolvedAuth = opts.getResolvedAuth ?? (() => resolvedAuth);\n'
            '\tconst openAiCompatEnabled = openAiChatCompletionsEnabled || openResponsesEnabled;\n'
            '\tasync function handleRequest(req, res) {\n'
            '\t\tconst scopedRequestPath = new URL(req.url ?? "/", "http://localhost").pathname;\n'
            '\t\tconst requestStages = [{\n'
            '\t\t\t\tname: "gateway-probes",\n'
            '\t\t\t\trun: () => handleGatewayProbeRequest(req, res, scopedRequestPath, resolvedAuth, trustedProxies, allowRealIpFallback, getReadiness)\n'
            '\t\t\t}, {\n'
            '\t\t\t\tname: "hooks",\n'
            '\t\t\t\trun: () => handleHooksRequest(req, res)\n'
            '\t\t\t}];\n'
            '\t\t\tif (openAiCompatEnabled && isOpenAiModelsPath(scopedRequestPath)) requestStages.push({\n'
            '\t\t\tname: "models",\n'
            '\t\t\trun: async () => (await getModelsHttpModule()).handleOpenAiModelsHttpRequest(req, res, {\n'
            '\t\t\t\tauth: resolvedAuth,\n'
            '\t\t\t\ttrustedProxies,\n'
            '\t\t\t\tallowRealIpFallback,\n'
            '\t\t\t\trateLimiter\n'
            '\t\t\t})\n'
            '\t\t});\n'
            '\t}\n'
            '}\n'
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
        server_source = server_bundle.read_text()
        assert 'async function handleWorkspaceFilesProxyRequest(req, res) {' in server_source
        assert 'name: "workspace-files-proxy"' in server_source
        assert 'run: () => handleWorkspaceFilesProxyRequest(req, res)' in server_source
        assert 'if (openAiCompatEnabled && isOpenAiModelsPath(scopedRequestPath)) requestStages.push({' in server_source


def test_bootstrap_patches_workspace_proxy_stage_for_upstream_2026_6_1_shape():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        gateway_bundle = dist_dir / "gateway-cli-test.js"
        server_bundle = dist_dir / "server.impl-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);\n'
            '}\n'
            'if (isLoopbackAddress(remoteAddr)) return { reason: "trusted_proxy_loopback_source" };\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\treturn true;\n'
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
        server_bundle.write_text(
            'function createGatewayHttpServer(opts) {\n'
            '\tconst { canvasHost, clients, controlUiEnabled, controlUiBasePath, controlUiRoot, openAiChatCompletionsEnabled, openAiChatCompletionsConfig, openResponsesEnabled, openResponsesConfig, strictTransportSecurityHeader, handleHooksRequest, handlePluginRequest, shouldEnforcePluginGatewayAuth, resolvedAuth, rateLimiter, getReadiness } = opts;\n'
            '\tconst getResolvedAuth = opts.getResolvedAuth ?? (() => resolvedAuth);\n'
            '\tconst openAiCompatEnabled = openAiChatCompletionsEnabled || openResponsesEnabled;\n'
            '\tasync function handleRequest(req, res) {\n'
            '\t\tconst scopedNodeCapability = normalizePluginNodeCapabilityScopedUrl(req.url ?? "/");\n'
            '\t\tif (scopedNodeCapability.rewrittenUrl) req.url = scopedNodeCapability.rewrittenUrl;\n'
            '\t\tconst scopedRequestPath = scopedNodeCapability.pathname;\n'
            '\t\tconst resolvedAuthValue = getResolvedAuth();\n'
            '\t\tconst requestStages = [{\n'
            '\t\t\t\tname: "gateway-probes",\n'
            '\t\t\t\trun: () => handleGatewayProbeRequest(req, res, scopedRequestPath, resolvedAuthValue, trustedProxies, allowRealIpFallback, getReadiness)\n'
            '\t\t\t}, {\n'
            '\t\t\t\tname: "hooks",\n'
            '\t\t\t\trun: () => handleHooksRequest(req, res)\n'
            '\t\t\t}];\n'
            '\t\t\tif (openAiCompatEnabled && isOpenAiModelsPath(scopedRequestPath)) requestStages.push({\n'
            '\t\t\tname: "models",\n'
            '\t\t\trun: async () => (await getModelsHttpModule()).handleOpenAiModelsHttpRequest(req, res, {\n'
            '\t\t\t\tauth: resolvedAuthValue,\n'
            '\t\t\t\ttrustedProxies,\n'
            '\t\t\t\tallowRealIpFallback,\n'
            '\t\t\t\trateLimiter\n'
            '\t\t\t})\n'
            '\t\t});\n'
            '\t}\n'
            '}\n'
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
        server_source = server_bundle.read_text()
        assert 'async function handleWorkspaceFilesProxyRequest(req, res) {' in server_source
        assert 'name: "workspace-files-proxy"' in server_source
        assert 'run: () => handleWorkspaceFilesProxyRequest(req, res)' in server_source
        assert 'auth: resolvedAuthValue' in server_source


def test_bootstrap_patches_workspace_proxy_stage_for_upstream_2026_3_28_shape():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        gateway_bundle = dist_dir / "gateway-cli-test.js"
        server_bundle = dist_dir / "gateway-cli-old-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);\n'
            '}\n'
            'if (isLoopbackAddress(remoteAddr)) return { reason: "trusted_proxy_loopback_source" };\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\treturn true;\n'
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
        )
        server_bundle.write_text(
            'function createGatewayHttpServer(opts) {\n'
            '\tasync function handleRequest(req, res) {\n'
            '\t\tconst requestPath = new URL(req.url ?? "/", "http://localhost").pathname;\n'
            '\t\tconst requestStages = [\n'
            '\t\t\t\t{\n'
            '\t\t\t\t\tname: "hooks",\n'
            '\t\t\t\t\trun: () => handleHooksRequest(req, res)\n'
            '\t\t\t\t},\n'
            '\t\t\t\t{\n'
            '\t\t\t\t\tname: "models",\n'
            '\t\t\t\t\trun: () => openAiCompatEnabled ? handleOpenAiModelsHttpRequest(req, res, {\n'
            '\t\t\t\t\t\tauth: resolvedAuth,\n'
            '\t\t\t\t\t\ttrustedProxies,\n'
            '\t\t\t\t\t\tallowRealIpFallback,\n'
            '\t\t\t\t\t\trateLimiter\n'
            '\t\t\t\t\t}) : false\n'
            '\t\t\t\t},\n'
            '\t\t];\n'
            '\t}\n'
            '}\n'
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
        server_source = server_bundle.read_text()
        assert 'name: "workspace-files-proxy"' in server_source
        assert 'run: () => handleWorkspaceFilesProxyRequest(req, res)' in server_source
        assert 'name: "models"' in server_source


def test_bootstrap_accepts_upstream_2026_3_28_loopback_gateway_runtime_logic():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        auth_bundle = dist_dir / "gateway-auth-test.js"
        connect_policy_bundle = dist_dir / "connect-policy-test.js"
        gateway_call_bundle = dist_dir / "call-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        auth_bundle.write_text(
            'function authorizeTrustedProxy(params) {\n'
            '\tconst { req, trustedProxies, trustedProxyConfig } = params;\n'
            '\tif (!req) return { reason: "trusted_proxy_no_request" };\n'
            '\tconst remoteAddr = req.socket?.remoteAddress;\n'
            '\tif (!remoteAddr || !isTrustedProxyAddress$1(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };\n'
            '\tconst userHeaderValue = headerValue(req.headers[trustedProxyConfig.userHeader.toLowerCase()]);\n'
            '\treturn { user: userHeaderValue.trim() };\n'
            '}\n'
        )
        connect_policy_bundle.write_text(
            'function shouldSkipControlUiPairing(policy, role, trustedProxyAuthOk = false, authMode) {\n'
            '\tif (trustedProxyAuthOk) {\n'
            '\t\treturn true;\n'
            '\t}\n'
            '\treturn role === "operator" && policy.allowBypass;\n'
            '}\n'
        )
        gateway_call_bundle.write_text(
            'function ensureExplicitGatewayAuth(params) {\n'
            '\tif (!params.urlOverride) return;\n'
            '\tconst explicitToken = params.explicitAuth?.token;\n'
            '}\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert 'if (!remoteAddr || !isTrustedProxyAddress$1(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };' in auth_bundle.read_text()
        assert 'if (trustedProxyAuthOk) {' in connect_policy_bundle.read_text()
        assert 'const parsed = new URL(params.urlOverride);' in gateway_call_bundle.read_text()


def test_bootstrap_disables_container_self_update_runtime_hooks():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        control_ui_assets_dir = dist_dir / "control-ui" / "assets"
        control_ui_assets_dir.mkdir(parents=True, exist_ok=True)
        client_bundle = dist_dir / "reply-test.js"
        gateway_bundle = dist_dir / "gateway-cli-test.js"
        server_bundle = dist_dir / "server-test.js"
        control_ui_bundle = control_ui_assets_dir / "main-test.js"

        client_bundle.write_text('const wsOptions = { maxPayload: 25 * 1024 * 1024 };')
        gateway_bundle.write_text(
            'function shouldSkipBackendSelfPairing(params) {\n'
            '\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;\n'
            '\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";\n'
            '\tconst usesDeviceTokenAuth = params.authMethod === "device-token";\n'
            '\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);\n'
            '}\n'
            'if (isLoopbackAddress(remoteAddr)) return { reason: "trusted_proxy_loopback_source" };\n'
            'function shouldAttachDeviceIdentityForGatewayCall(params) {\n'
            '\treturn true;\n'
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
        server_bundle.write_text(
            'let updateAvailableCache = null;\n'
            'function getUpdateAvailable() {\n'
            '\treturn updateAvailableCache;\n'
            '}\n'
            'function scheduleGatewayUpdateCheck(params) {\n'
            '\tlet stopped = false;\n'
            '\tlet timer = null;\n'
            '\tlet running = false;\n'
            '\tconst tick = async () => {\n'
            '\t\tif (stopped || running) return;\n'
            '\t\trunning = true;\n'
            '\t\ttry {\n'
            '\t\t\tawait runGatewayUpdateCheck(params);\n'
            '\t\t} catch {} finally {\n'
            '\t\t\trunning = false;\n'
            '\t\t}\n'
            '\t\tif (stopped) return;\n'
            '\t\tconst intervalMs = resolveCheckIntervalMs(params.cfg);\n'
            '\t\ttimer = setTimeout(() => {\n'
            '\t\t\ttick();\n'
            '\t\t}, intervalMs);\n'
            '\t};\n'
            '\ttick();\n'
            '\treturn () => {\n'
            '\t\tstopped = true;\n'
            '\t\tif (timer) {\n'
            '\t\t\tclearTimeout(timer);\n'
            '\t\t\ttimer = null;\n'
            '\t\t}\n'
            '\t};\n'
            '}\n'
        )
        control_ui_bundle.write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_WORKSPACE_FILES_ENABLED"] = "0"

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        patched_source = server_bundle.read_text()
        assert 'function getUpdateAvailable() {\n\treturn null;\n}' in patched_source
        assert 'function scheduleGatewayUpdateCheck(params) {\n\treturn () => {};\n}' in patched_source


def test_bootstrap_fails_when_required_runtime_patch_targets_are_missing():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        dist_dir = Path(tmpdir) / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        marker_file = dist_dir / ".agentengine-dist-marker"
        (dist_dir / "control-ui-only.js").write_text('this.ws.addEventListener(`open`,()=>this.queueConnect())')

        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DIST_DIR"] = str(dist_dir)
        env["OPENCLAW_DIST_PATCH_MARKER"] = str(marker_file)

        result = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        combined = result.stderr or result.stdout
        assert (
            "required dist patches missing:" in combined
            or "必需的 dist 补丁缺失:" in combined
        )
        assert not marker_file.exists()


def test_bootstrap_dist_patch_registry_uses_capability_group_and_variant_metadata():
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert "const requiredCapabilities = new Set([" in bootstrap
    assert "capability:" in bootstrap
    assert "group:" in bootstrap
    assert "variant:" in bootstrap
    assert "why:" in bootstrap
    assert "since:" in bootstrap
    assert "按能力验证必需补丁" in bootstrap
    assert "缺失的必需能力" in bootstrap
    assert "requiredLabels" not in bootstrap
    assert "patchedLabels" not in bootstrap


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
        env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5.1"
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


def test_bootstrap_applies_mem0_memory_backend_manifest():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        default_extensions_dir = Path(tmpdir) / "default-extensions" / "openclaw-mem0"
        default_extensions_dir.mkdir(parents=True, exist_ok=True)
        (default_extensions_dir / "manifest.json").write_text('{"name":"openclaw-mem0"}\n')
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["OPENCLAW_DEFAULT_EXTENSIONS_DIR"] = str(Path(tmpdir) / "default-extensions")
        env["MEMORY_BACKEND_MANIFEST"] = _build_mem0_manifest_json()
        env["MEM0_API_KEY"] = f"2000104981.{VALID_MEM0_UUID}:mem0-secret"
        env["MEM0_USER_ID"] = "2000104981"
        env["MEM0_BASE_URL"] = "http://mem-service.example.test"

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
        assert (Path(tmpdir) / "extensions" / "openclaw-mem0" / "manifest.json").exists()
        assert cfg["plugins"]["slots"]["memory"] == "openclaw-mem0"
        assert "openclaw-mem0" in cfg["plugins"]["allow"]
        assert cfg["plugins"]["entries"]["openclaw-mem0"] == {
            "enabled": True,
            "config": {
                "mode": "platform",
                "apiKey": f"2000104981.{VALID_MEM0_UUID}:mem0-secret",
                "baseUrl": "http://mem-service.example.test",
                "userId": "2000104981",
            },
        }


def test_bootstrap_openclaw_default_manifest_clears_existing_mem0_memory_backend():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "plugins": {
                        "slots": {"memory": "openclaw-mem0", "search": "perplexity"},
                        "allow": ["openclaw-mem0", "perplexity"],
                        "entries": {
                            "openclaw-mem0": {
                                "enabled": True,
                                "config": {
                                    "mode": "platform",
                                    "apiKey": "old-key",
                                    "baseUrl": "http://mem-service.example.test",
                                    "userId": "2000104981",
                                },
                            },
                            "perplexity": {"enabled": True},
                        },
                    }
                }
            )
        )
        env = _build_base_env(tmpdir, str(config_path))
        env["OPENCLAW_MODEL_API_KEY"] = "dummy-secret-value"
        env["MEMORY_BACKEND_MANIFEST"] = _build_openclaw_default_memory_manifest_json()

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
        assert cfg["plugins"]["slots"] == {"search": "perplexity"}
        assert "openclaw-mem0" not in cfg["plugins"]["allow"]
        assert cfg["plugins"]["entries"]["openclaw-mem0"] == {
            "enabled": False,
            "config": {},
        }
        assert cfg["plugins"]["entries"]["perplexity"] == {"enabled": True}


def test_bootstrap_fails_when_mem0_manifest_env_is_incomplete():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "openclaw.json"
        env = _build_base_env(tmpdir, str(config_path))
        env["MEMORY_BACKEND_MANIFEST"] = _build_mem0_manifest_json()

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
        assert (
            "MEM0_API_KEY" in combined
            or "MEM0_USER_ID" in combined
            or "MEM0_BASE_URL" in combined
        )
