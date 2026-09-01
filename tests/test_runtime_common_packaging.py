import importlib
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    tomllib = importlib.import_module("tomllib")
else:
    tomllib = importlib.import_module("tomli")

from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.container_builder import ContainerBuilder
from ksadk.detection import DetectionResult, FrameworkType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build_wheel_in_isolated_source(tmp_path: Path) -> Path:
    source_dir = tmp_path / "source"
    wheel_dir = tmp_path / "wheel"
    source_dir.mkdir()
    wheel_dir.mkdir()

    for filename in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy2(REPO_ROOT / filename, source_dir / filename)

    def ignore_generated_files(directory: str, names: list[str]) -> set[str]:
        ignored = {
            name
            for name in names
            if name in {"__pycache__", "build", "dist"}
            or name.endswith(".egg-info")
            or name.endswith((".pyc", ".pyo"))
        }
        if Path(directory) == REPO_ROOT / "ksadk" / "server":
            ignored.add("static")
        return ignored

    for package_dir in ("ksadk", "ksadk_runtime_common"):
        shutil.copytree(
            REPO_ROOT / package_dir,
            source_dir / package_dir,
            ignore=ignore_generated_files,
        )

    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-build-logs",
            "--no-create-gitignore",
            "--out-dir",
            str(wheel_dir),
            str(source_dir),
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    wheels = list(wheel_dir.glob("ksadk-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_pyproject_uses_in_repo_runtime_common_source_package():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "agentengine-runtime-common" not in pyproject
    assert "ksadk_runtime_common*" in pyproject


def test_runtime_common_workspace_router_is_python310_compatible(tmp_path: Path):
    router_source = (
        REPO_ROOT / "ksadk_runtime_common" / "workspace_files" / "router.py"
    ).read_text(encoding="utf-8")
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


def test_built_wheel_excludes_studio_frontend_sources_and_node_modules():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        leaked = [
            name
            for name in archive.namelist()
            if name.startswith(("ksadk/studio/web/", "ksadk/studio/react-ui/"))
        ]

    assert leaked == []


def test_built_wheel_includes_synced_web_static_entrypoint():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())

    assert "ksadk/server/static/index.html" in names
    assert any(name.startswith("ksadk/server/static/assets/") for name in names)


def test_built_wheel_includes_react_studio_static_entrypoint():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())
        studio_index = archive.read("ksadk/studio/static/index.html").decode("utf-8")

    assert '<div id="root"></div>' in studio_index
    assert "/static/assets/" in studio_index
    assert "ksadk/studio/static/shared-chat.css" not in names
    assert any(name.startswith("ksadk/studio/static/assets/") for name in names)


def test_built_sdist_includes_react_studio_static_entrypoint():
    sdists = sorted((REPO_ROOT / "dist").glob("ksadk-*.tar.gz"))
    assert sdists, "请先运行受控构建生成 dist/ksadk-*.tar.gz"

    with tarfile.open(sdists[-1]) as archive:
        names = set(archive.getnames())

    assert any(name.endswith("/ksadk/studio/static/index.html") for name in names)
    assert any("/ksadk/studio/static/assets/" in name for name in names)


def test_release_build_generates_ignored_react_studio_static_assets():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "ksadk/studio/static/**" in gitignore
    studio_source = REPO_ROOT / "ksadk/studio/react-ui"
    if studio_source.exists():
        tracked_static_files = subprocess.run(
            ["git", "ls-files", "ksadk/studio/static"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert tracked_static_files == []
    else:
        assert (REPO_ROOT / "ksadk/studio/static/index.html").is_file()
        assert any((REPO_ROOT / "ksadk/studio/static/assets").iterdir())
    assert "STUDIO_REACT_DIR := ksadk/studio/react-ui" in makefile
    assert "STUDIO_STATIC_DIR := ksadk/studio/static" in makefile
    target = makefile.split("build-studio-static:\n", 1)[1].split("\n\n", 1)[0]
    assert "set -eu" in target
    assert '$(KSADK_WEB_NPM) --prefix "$(STUDIO_REACT_DIR)" ci' in target
    assert 'WEB_TARBALL_PATH="$(KSADK_WEB_TARBALL)"' in target
    assert (
        '$(KSADK_WEB_NPM) --prefix "$(STUDIO_REACT_DIR)" install '
        '--no-save --package-lock=false "$$WEB_TARBALL_PATH"' in target
    )
    assert 'cat "$(KSADK_WEB_CACHE_DIR)/.tarball-name"' not in target
    assert 'npm --prefix "$(STUDIO_REACT_DIR)" run build' in target
    assert '$(STUDIO_STATIC_DIR)/index.html' in target
    build_target = makefile.split("build: check-build-deps", 1)[1].split("\n", 1)[0]
    public_build_target = makefile.split("public-build-check: clean-dist", 1)[1].split(
        "\n", 1
    )[0]
    assert "build-studio-static" in build_target
    assert "build-studio-static" in public_build_target
    assert "build-studio-static" in makefile.split("build-frontend:", 1)[1].split("\n", 1)[0]


def test_ci_installs_node_before_building_generated_studio_static_assets():
    ci_workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release_workflow = (REPO_ROOT / ".github/workflows/release-check.yml").read_text(
        encoding="utf-8"
    )

    adk_matrix_job = ci_workflow.split("  test-adk-matrix:\n", 1)[1].split(
        "  test-", 1
    )[0]
    assert "actions/setup-node@v4" in adk_matrix_job
    assert "make build-frontend" in adk_matrix_job
    assert "actions/setup-node@v4" in release_workflow
    assert "make build-frontend" in release_workflow


def test_built_wheel_excludes_legacy_web_ui_sources_and_build_outputs():
    wheels = sorted((REPO_ROOT / "dist").glob("ksadk-*.whl"))
    assert wheels, "请先运行 uv build 生成 dist/ksadk-*.whl"

    with zipfile.ZipFile(wheels[-1]) as archive:
        leaked = [name for name in archive.namelist() if name.startswith("ksadk/server/web-ui/")]

    assert leaked == []


def test_pyproject_keeps_generated_static_as_package_data():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]["ksadk"]
    assert "server/static/**/*" in package_data
    assert "studio/static/**/*" in package_data
    assert all("server/web-ui" not in entry for entry in package_data)


def test_pyproject_excludes_non_python_frontend_sources_from_package_discovery():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    package_find = pyproject["tool"]["setuptools"]["packages"]["find"]
    assert "ksadk.server.web-ui*" in package_find["exclude"]
    assert "ksadk.studio.react-ui*" in package_find["exclude"]
    assert "ksadk.studio.web*" not in package_find["exclude"]


def test_react_is_the_only_studio_frontend_source_tree():
    studio_root = REPO_ROOT / "ksadk/studio"

    assert not (studio_root / "web").exists()
    assert not (studio_root / "static-react").exists()
    if (studio_root / "react-ui").exists():
        assert (studio_root / "react-ui/src/main.tsx").is_file()
        assert (studio_root / "react-ui/src/studio.css").is_file()
    else:
        assert not (studio_root / "react-ui").exists()
        assert (studio_root / "static/index.html").is_file()


def test_pyproject_declares_python_multipart_for_local_web_ui_uploads():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-multipart>=0.0.9,<1.0.0" in pyproject


def test_pyproject_declares_python_socks_for_openclaw_gateway_proxy_support():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-socks>=2.7.1,<3.0.0" in pyproject


def test_pyproject_declares_tomli_for_python310_plugin_package_parsing():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    requirements = [Requirement(item) for item in project["dependencies"]]
    tomli = next(item for item in requirements if item.name == "tomli")

    assert str(tomli.specifier) == ">=2.0.0"
    assert tomli.marker is not None
    assert str(tomli.marker) == 'python_version < "3.11"'


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
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert "fastapi>=0.100.0,<1.0.0" in dependencies
    # Studio 是基础入口；ADK 本体必须随基础包安装，额外生成依赖仍保持在 [adk]。
    assert "google-adk>=1.34.0,<3.0.0" in dependencies
    assert any(item.startswith("litellm>=") for item in optional_dependencies["adk"])
    # LangChain 生态下限锚定本地已验证版本(不降级,<2.0 守 1.x 稳定线)
    assert "langchain>=1.3.14,<2.0.0" in dependencies
    assert "langchain-core>=1.5.0,<2.0.0" in dependencies
    assert "langgraph>=1.2.0,<1.3.0" in dependencies
    assert (
        "deepagents>=0.6.2,<1.0.0; python_version >= '3.11'" in optional_dependencies["deepagents"]
    )
    assert "fastapi>=0.100.0,<0.124.0" not in dependencies


def test_pyproject_makes_langchain_openai_framework_optional():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    mandatory_names = {Requirement(dependency).name for dependency in dependencies}
    assert "langchain-openai" not in mandatory_names

    for extra in ("langchain", "langgraph", "deepagents"):
        assert "langchain-openai>=1.4.0,<2.0.0" in optional_dependencies[extra]


def test_built_wheel_makes_langchain_openai_framework_optional(tmp_path: Path):
    wheel_path = _build_wheel_in_isolated_source(tmp_path)

    with zipfile.ZipFile(wheel_path) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))

    assert metadata["Version"] == "0.8.3"
    requirements = [Requirement(raw) for raw in metadata.get_all("Requires-Dist", [])]
    assert all(
        requirement.name != "langchain-openai" or requirement.marker is not None
        for requirement in requirements
    )

    for extra in ("langchain", "langgraph", "deepagents"):
        matching_requirements = [
            requirement
            for requirement in requirements
            if requirement.name == "langchain-openai"
            and str(requirement.specifier) == "<2.0.0,>=1.4.0"
            and str(requirement.marker) == f'extra == "{extra}"'
        ]
        assert matching_requirements


def test_repo_root_dockerignore_excludes_local_build_artifacts():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in [".git", ".venv", "dist", "build", "__pycache__"]:
        assert entry in dockerignore


def test_makefile_delegates_runtime_image_builds_to_agentengine_images_repo():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "AGENTENGINE_IMAGES_DIR ?= ../agentengine-images" in makefile
    assert '$(MAKE) -C "$(AGENTENGINE_IMAGES_DIR)" $@' in makefile
    assert "-f deploy/openclaw/Dockerfile" not in makefile
    assert "-f deploy/hermes/Dockerfile" not in makefile


def test_runtime_templates_initialize_tracing_for_otlp_envs(tmp_path: Path):
    detection_result = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo_agent",
        entry_point="demo_agent/agent.py",
        package_path=str(tmp_path / "demo_agent"),
        agent_variable="root_agent",
    )
    templates = [
        CodeBuilder(tmp_path)._generate_entrypoint(detection_result),
        ContainerBuilder(tmp_path)._generate_entrypoint(detection_result, "demo_agent"),
    ]

    for source in templates:
        compile(source, "entrypoint.py", "exec")
        normalized_source = " ".join(source.split())

        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in source
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in source
        assert "OTEL_EXPORTER_OTLP_HEADERS" in source
        assert "OTEL_EXPORTER_OTLP_TRACES_HEADERS" in source
        assert "CLOUD_MONITOR_APP_KEY" in source
        assert "CLOUD_MONITOR_OTLP_ENDPOINT" in source
        assert "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT" in source
        assert "CLOUD_MONITOR_OTLP_HEADERS" in source
        assert "CLOUD_MONITOR_OTLP_TRACES_HEADERS" in source
        assert "CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY" not in source
        assert "CLOUD_MONITOR_LANGFUSE_SECRET_KEY" not in source
        assert "CLOUD_MONITOR_LANGFUSE_HOST" not in source
        assert "CLOUD_MONITOR_OTLP_ENABLED" not in source
        assert "use_callback_only" not in source
        assert "has_cloud_monitor_langfuse" not in source
        assert (
            'os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") '
            'or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")'
        ) in normalized_source
        assert "setup_tracing()" in source
