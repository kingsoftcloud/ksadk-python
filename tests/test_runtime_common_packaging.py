from pathlib import Path
import zipfile
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_uses_in_repo_runtime_common_source_package():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "agentengine-runtime-common" not in pyproject
    assert "ksadk_runtime_common*" in pyproject


def test_runtime_common_schema_uses_public_identifier():
    schema = (
        REPO_ROOT / "ksadk_runtime_common" / "schemas" / "memory_backend_manifest.schema.json"
    ).read_text(encoding="utf-8")

    assert "https://kingsoftcloud.github.io/ksadk-python/" in schema
    assert "ezone.ksyun.com" not in schema


def test_runtime_common_workspace_router_is_python310_compatible(tmp_path: Path):
    router_source = (REPO_ROOT / "ksadk_runtime_common" / "workspace_files" / "router.py").read_text(
        encoding="utf-8"
    )
    assert "from datetime import UTC" not in router_source
    assert "datetime.UTC" not in router_source

    from ksadk_runtime_common.workspace_files.router import _isoformat_timestamp

    target = tmp_path / "demo.txt"
    target.write_text("ok", encoding="utf-8")

    assert _isoformat_timestamp(target).endswith("Z")


def test_distributed_python_sources_do_not_use_python311_datetime_utc():
    package_roots = [
        REPO_ROOT / "ksadk",
        REPO_ROOT / "ksadk_runtime_common",
    ]
    offenders: list[str] = []

    for package_root in package_roots:
        for source_path in package_root.rglob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            if "from datetime import UTC" in source or "datetime.UTC" in source:
                offenders.append(str(source_path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_built_wheel_excludes_web_ui_node_modules():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        leaked = [
            name
            for name in archive.namelist()
            if name.startswith("ksadk/server/web-ui/node_modules/")
        ]

    assert leaked == []


def test_built_wheel_includes_synced_web_static_entrypoint():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())

    assert "ksadk/server/static/index.html" in names
    assert any(name.startswith("ksadk/server/static/assets/") for name in names)


def test_built_wheel_excludes_legacy_web_ui_sources_and_build_outputs():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        leaked = [
            name
            for name in archive.namelist()
            if name.startswith("ksadk/server/web-ui/")
        ]

    assert leaked == []


def test_pyproject_keeps_only_synced_static_as_ksadk_web_package_data():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]["ksadk"]
    assert "server/static/**/*" in package_data
    assert all("server/web-ui" not in entry for entry in package_data)


def test_pyproject_declares_python_multipart_for_local_web_ui_uploads():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-multipart>=0.0.9,<1.0.0" in pyproject


def test_pyproject_declares_python_socks_for_openclaw_gateway_proxy_support():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-socks>=2.7.1,<3.0.0" in pyproject


def test_pyproject_declares_kingsoftcloud_sdk_as_default_dependency():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "kingsoftcloud-sdk-python>=1.5.8.94" in pyproject["project"]["dependencies"]


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


def test_makefile_delegates_runtime_image_builds_to_agentengine_images_repo():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "AGENTENGINE_IMAGES_DIR ?= ../agentengine-images" in makefile
    assert "$(MAKE) -C \"$(AGENTENGINE_IMAGES_DIR)\" $@" in makefile
    assert "-f deploy/openclaw/Dockerfile" not in makefile
    assert "-f deploy/hermes/Dockerfile" not in makefile


def test_runtime_templates_initialize_tracing_for_generic_otlp_env():
    for rel_path in ["ksadk/builders/code_builder.py", "ksadk/builders/container_builder.py"]:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")

        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in source
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in source
        assert 'os.environ.get("LANGFUSE_PUBLIC_KEY") or has_otlp' in source
