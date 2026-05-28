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


def test_pyproject_declares_asyncpg_for_postgres_session_backend():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "asyncpg>=0.30.0,<1.0.0" in pyproject


def test_pyproject_declares_greenlet_for_adk_database_session_backend():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "greenlet>=1.0.0" in pyproject


def test_pyproject_declares_validated_framework_dependency_windows():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "fastapi>=0.100.0,<1.0.0" in pyproject
    assert "google-adk>=1.34.0,<2.0.0" in pyproject
    assert "langchain>=1.3.0,<2.0.0" in pyproject
    assert "langchain-core>=1.4.0,<2.0.0" in pyproject
    assert "langchain-openai>=1.2.0,<2.0.0" in pyproject
    assert "langgraph>=1.2.0,<1.3.0" in pyproject
    assert "deepagents>=0.6.2,<1.0.0" in pyproject
    assert "fastapi>=0.100.0,<0.124.0" not in pyproject


def test_repo_root_dockerignore_excludes_local_build_artifacts():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in [".git", ".venv", "dist", "build", "__pycache__"]:
        assert entry in dockerignore


def test_public_makefile_keeps_runtime_image_building_out_of_repo_root():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "OPENCLAW_CONTEXT := ." not in makefile
    assert "HERMES_CONTEXT := ." not in makefile
    assert "-f deploy/openclaw/Dockerfile" not in makefile
    assert "-f deploy/hermes/Dockerfile" not in makefile
