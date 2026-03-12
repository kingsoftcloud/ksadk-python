import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "openclaw" / "bootstrap.sh"


def _build_base_env(state_dir: str, config_path: str) -> dict:
    env = os.environ.copy()
    env.pop("OPENCLAW_MODEL_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env["OPENCLAW_STATE_DIR"] = state_dir
    env["OPENCLAW_CONFIG_PATH"] = config_path
    env["OPENCLAW_BOOTSTRAP_ONLY"] = "1"
    env["OPENCLAW_MODEL_PROVIDER_ID"] = "ksyun"
    env["OPENCLAW_MODEL_BASE_URL"] = "http://example.test/v1"
    env["OPENCLAW_DEFAULT_MODEL"] = "ksyun/glm-5"
    return env


def test_bootstrap_writes_secretref_for_model_api_key():
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
        assert "missing secret env: OPENCLAW_MODEL_API_KEY" in combined
