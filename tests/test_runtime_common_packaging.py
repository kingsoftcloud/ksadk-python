from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_uses_in_repo_runtime_common_source_package():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "agentengine-runtime-common" not in pyproject
    assert "ksadk_runtime_common*" in pyproject


def test_pyproject_declares_python_multipart_for_local_web_ui_uploads():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-multipart>=0.0.9,<1.0.0" in pyproject


def test_pyproject_declares_python_socks_for_openclaw_gateway_proxy_support():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-socks>=2.7.1,<3.0.0" in pyproject


def test_repo_root_dockerignore_excludes_local_build_artifacts():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in [".git", ".venv", "dist", "build", "__pycache__"]:
        assert entry in dockerignore


def test_makefile_builds_runtime_images_from_repo_root_context():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "OPENCLAW_CONTEXT := ." in makefile
    assert "HERMES_CONTEXT := ." in makefile
    assert "-f deploy/openclaw/Dockerfile" in makefile
    assert "-f deploy/hermes/Dockerfile" in makefile
