from types import SimpleNamespace

from ksadk.builders import container_builder
from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.container_builder import ContainerBuilder
from ksadk.builders.mcp_builder import MCPCodeBuilder
from ksadk.deployment.manager import K8sDeployer
from ksadk.detection import DetectionResult, FrameworkType


def _detection_result(framework: str):
    return SimpleNamespace(type=SimpleNamespace(value=framework))


def _full_detection_result(framework_type: FrameworkType):
    return DetectionResult(
        type=framework_type,
        name="demo_agent",
        entry_point="demo_agent/agent.py",
        package_path="/tmp/demo_agent",
        agent_variable="root_agent",
    )


def test_ensure_docker_running_prints_windows_docker_desktop_hint(monkeypatch, capsys):
    monkeypatch.setattr(container_builder.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(container_builder.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        container_builder.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    assert container_builder.ensure_docker_running() is False
    output = capsys.readouterr().out
    assert "Docker Desktop" in output
    assert "systemctl" not in output


def test_code_builder_prefers_user_pins_over_base_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nuvicorn==0.38.0\npython-dotenv==1.2.1\n",
        encoding="utf-8",
    )
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "fastapi==0.121.2" in deps
    assert "uvicorn==0.38.0" in deps
    assert "python-dotenv==1.2.1" in deps
    assert "fastapi>=0.100.0" not in deps
    assert "uvicorn>=0.23.0" not in deps
    assert "python-dotenv>=1.0.0" not in deps


def test_container_builder_prefers_user_pins_over_base_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nuvicorn==0.38.0\npython-dotenv==1.2.1\n",
        encoding="utf-8",
    )
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "fastapi==0.121.2" in deps
    assert "uvicorn==0.38.0" in deps
    assert "python-dotenv==1.2.1" in deps
    assert "fastapi>=0.100.0" not in deps
    assert "uvicorn>=0.23.0" not in deps
    assert "python-dotenv>=1.0.0" not in deps


def test_mcp_builder_prefers_user_pins_over_base_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "uvicorn==0.38.0\npython-dotenv==1.2.1\n",
        encoding="utf-8",
    )
    builder = MCPCodeBuilder(tmp_path)
    builder.build_dir.mkdir(parents=True, exist_ok=True)

    requirements_path = builder._prepare_mcp_requirements(SimpleNamespace())
    deps = requirements_path.read_text(encoding="utf-8").splitlines()

    assert "uvicorn==0.38.0" in deps
    assert "python-dotenv==1.2.1" in deps
    assert "uvicorn>=0.23.0" not in deps
    assert "python-dotenv>=1.0.0" not in deps


def test_code_builder_omits_bundled_ksadk_package_from_runtime_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nksadk==0.4.0\n",
        encoding="utf-8",
    )
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "fastapi==0.121.2" in deps
    assert "ksadk==0.4.0" not in deps
    assert all(not dep.startswith("ksadk") for dep in deps)
    assert "a2a-sdk>=0.3.22" in deps
    assert "requests-aws4auth>=1.2.0" in deps


def test_container_builder_omits_bundled_ksadk_package_from_runtime_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.121.2\nksadk==0.4.0\n",
        encoding="utf-8",
    )
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "fastapi==0.121.2" in deps
    assert "ksadk==0.4.0" not in deps
    assert all(not dep.startswith("ksadk") for dep in deps)
    assert "a2a-sdk>=0.3.22" in deps
    assert "requests-aws4auth>=1.2.0" in deps


def test_code_builder_bundles_attachment_runtime_requirements_without_optional_backends(tmp_path):
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
    assert "rapidocr-onnxruntime>=1.2.0" not in deps
    assert "mcp>=1.1.0" not in deps
    assert "langchain-mcp-adapters>=0.0.1" not in deps
    assert "asyncpg>=0.30.0,<1.0.0" not in deps
    assert "boto3==1.40.61" not in deps
    assert "SQLAlchemy==2.0.44" not in deps
    assert "psycopg[binary]==3.3.0" not in deps
    assert "psycopg-pool==3.3.0" not in deps
    assert "pandas==2.2.2" not in deps
    assert "openpyxl==3.1.5" not in deps
    assert "xlrd==2.0.2" not in deps
    assert "python-pptx==1.0.2" not in deps
    assert "docx2python==3.5.0" not in deps


def test_code_builder_includes_mcp_runtime_when_project_uses_langchain_mcp_adapter(tmp_path):
    (tmp_path / "agent.py").write_text(
        "from langchain_mcp_adapters.client import MultiServerMCPClient\n",
        encoding="utf-8",
    )
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "mcp>=1.1.0" in deps
    assert "langchain-mcp-adapters>=0.0.1" in deps


def test_code_builder_includes_mcp_runtime_when_env_declares_mcp_servers(tmp_path):
    (tmp_path / ".env").write_text('KSADK_MCP_SERVERS=[{"name":"demo","url":"http://mcp"}]\n', encoding="utf-8")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "mcp>=1.1.0" in deps
    assert "langchain-mcp-adapters>=0.0.1" in deps


def test_code_builder_does_not_include_mcp_runtime_for_empty_mcp_servers(tmp_path):
    (tmp_path / ".env").write_text("KSADK_MCP_SERVERS=[]\n", encoding="utf-8")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "mcp>=1.1.0" not in deps
    assert "langchain-mcp-adapters>=0.0.1" not in deps


def test_code_builder_ignores_cached_build_files_when_detecting_optional_imports(tmp_path):
    cached_dir = tmp_path / ".agentengine" / "code_build" / "old"
    cached_dir.mkdir(parents=True)
    (cached_dir / "agent.py").write_text(
        "from langchain_mcp_adapters.client import MultiServerMCPClient\n",
        encoding="utf-8",
    )
    (tmp_path / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "mcp>=1.1.0" not in deps
    assert "langchain-mcp-adapters>=0.0.1" not in deps


def test_code_builder_includes_mcp_runtime_when_build_flag_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_BUILD_ENABLE_MCP", "true")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "mcp>=1.1.0" in deps
    assert "langchain-mcp-adapters>=0.0.1" in deps


def test_code_builder_includes_asyncpg_when_postgres_session_declared(tmp_path):
    (tmp_path / ".env").write_text("KSADK_SESSION_BACKEND=postgres\n", encoding="utf-8")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "asyncpg>=0.30.0,<1.0.0" in deps


def test_code_builder_includes_asyncpg_when_postgres_dsn_declared(tmp_path):
    (tmp_path / ".env").write_text(
        "KSADK_SESSION_DSN=postgresql://user:pass@example.com/db\n",
        encoding="utf-8",
    )
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "asyncpg>=0.30.0,<1.0.0" in deps


def test_code_builder_includes_asyncpg_when_build_flag_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_BUILD_ENABLE_POSTGRES_SESSION", "true")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "asyncpg>=0.30.0,<1.0.0" in deps


def test_code_builder_includes_attachment_ocr_runtime_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_BUILD_ENABLE_ATTACHMENT_OCR", "true")
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
    assert "rapidocr-onnxruntime>=1.2.0" in deps


def test_container_builder_uses_same_optional_runtime_detection(tmp_path):
    (tmp_path / ".env").write_text(
        'KSADK_MCP_SERVERS=[{"name":"demo","url":"http://mcp"}]\nKSADK_SESSION_BACKEND=postgres\n',
        encoding="utf-8",
    )
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "mcp>=1.1.0" in deps
    assert "langchain-mcp-adapters>=0.0.1" in deps
    assert "asyncpg>=0.30.0,<1.0.0" in deps


def test_code_builder_uses_validated_langgraph_ecosystem_dependency_window(tmp_path):
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("deepagents"))

    assert "fastapi>=0.100.0,<1.0.0" in deps
    assert "langchain>=1.3.0,<2.0.0" in deps
    assert "langchain-core>=1.4.0,<2.0.0" in deps
    assert "langchain-openai>=1.2.0,<2.0.0" in deps
    assert "langgraph>=1.2.0,<1.3.0" in deps
    assert "deepagents>=0.6.2,<1.0.0" in deps
    assert "langgraph>=0.1.0" not in deps


def test_code_builder_uses_validated_adk_dependency_window(tmp_path):
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("adk"))

    assert "fastapi>=0.100.0,<1.0.0" in deps
    assert "google-adk>=1.34.0,<2.0.0" in deps
    assert "google-adk>=1.0.0" not in deps


def test_container_builder_bundles_attachment_runtime_requirements_without_optional_backends(tmp_path):
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
    assert "rapidocr-onnxruntime>=1.2.0" not in deps


def test_container_builder_includes_attachment_ocr_runtime_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_BUILD_ENABLE_ATTACHMENT_OCR", "true")
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps
    assert "rapidocr-onnxruntime>=1.2.0" in deps



def test_container_builder_uses_same_framework_dependency_windows(tmp_path):
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("deepagents"),
        tmp_path,
    ).splitlines()

    assert "fastapi>=0.100.0,<1.0.0" in deps
    assert "langchain>=1.3.0,<2.0.0" in deps
    assert "langchain-core>=1.4.0,<2.0.0" in deps
    assert "langchain-openai>=1.2.0,<2.0.0" in deps
    assert "langgraph>=1.2.0,<1.3.0" in deps
    assert "deepagents>=0.6.2,<1.0.0" in deps


def test_k8s_deployer_uses_same_framework_dependency_windows():
    deployer = K8sDeployer()

    deps = deployer._generate_requirements(_detection_result("deepagents")).splitlines()

    assert "fastapi>=0.100.0,<1.0.0" in deps
    assert "langchain>=1.3.0,<2.0.0" in deps
    assert "langchain-core>=1.4.0,<2.0.0" in deps
    assert "langchain-openai>=1.2.0,<2.0.0" in deps
    assert "langgraph>=1.2.0,<1.3.0" in deps
    assert "deepagents>=0.6.2,<1.0.0" in deps
    assert "boto3==1.40.61" not in deps
    assert "SQLAlchemy==2.0.44" not in deps
    assert "psycopg[binary]==3.3.0" not in deps
    assert "psycopg-pool==3.3.0" not in deps
    assert "pandas==2.2.2" not in deps
    assert "openpyxl==3.1.5" not in deps
    assert "xlrd==2.0.2" not in deps
    assert "python-pptx==1.0.2" not in deps
    assert "docx2python==3.5.0" not in deps


def test_code_builder_includes_bundled_attachment_runtime_requirements(tmp_path):
    builder = CodeBuilder(tmp_path)

    deps = builder._build_requirements_list(_detection_result("langgraph"))

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps


def test_container_builder_includes_bundled_attachment_runtime_requirements(tmp_path):
    builder = ContainerBuilder(tmp_path)

    deps = builder._generate_requirements(
        _detection_result("langgraph"),
        tmp_path,
    ).splitlines()

    assert "pypdf>=6.0.0" in deps
    assert "beautifulsoup4>=4.12.0" in deps


def test_code_builder_entrypoint_uses_otlp_direct_by_default_for_code_frameworks(tmp_path):
    builder = CodeBuilder(tmp_path)

    for framework_type in (
        FrameworkType.ADK,
        FrameworkType.LANGCHAIN,
        FrameworkType.LANGGRAPH,
        FrameworkType.DEEPAGENTS,
    ):
        entrypoint = builder._generate_entrypoint(_full_detection_result(framework_type))

        assert "LANGFUSE_USE_CALLBACK" in entrypoint
        assert "use_callback_only=is_langchain" not in entrypoint
        assert 'in ("LANGCHAIN", "LANGGRAPH", "DEEPAGENTS")' not in entrypoint


def test_code_builder_entrypoint_patches_langchain_before_loading_user_agent(tmp_path):
    builder = CodeBuilder(tmp_path)

    entrypoint = builder._generate_entrypoint(_full_detection_result(FrameworkType.LANGGRAPH))

    patch_index = entrypoint.index("apply_langchain_patch()")
    load_index = entrypoint.index("runner.load_agent()")
    assert patch_index < load_index


def test_code_builder_entrypoint_adds_src_layout_to_pythonpath(tmp_path):
    builder = CodeBuilder(tmp_path)

    entrypoint = builder._generate_entrypoint(_full_detection_result(FrameworkType.DEEPAGENTS))

    assert 'CODE_SRC = os.path.join(CODE_ROOT, "src")' in entrypoint
    assert "sys.path.insert(0, CODE_SRC)" in entrypoint


def test_container_builder_entrypoint_uses_otlp_direct_by_default_for_code_frameworks(tmp_path):
    builder = ContainerBuilder(tmp_path)

    for framework_type in (
        FrameworkType.ADK,
        FrameworkType.LANGCHAIN,
        FrameworkType.LANGGRAPH,
        FrameworkType.DEEPAGENTS,
    ):
        entrypoint = builder._generate_entrypoint(
            _full_detection_result(framework_type),
            "demo_agent",
        )

        assert "LANGFUSE_USE_CALLBACK" in entrypoint
        assert "use_callback_only=is_langchain" not in entrypoint
        assert 'in ("LANGCHAIN", "LANGGRAPH", "DEEPAGENTS")' not in entrypoint


def test_container_builder_entrypoint_patches_langchain_before_loading_user_agent(tmp_path):
    builder = ContainerBuilder(tmp_path)

    entrypoint = builder._generate_entrypoint(
        _full_detection_result(FrameworkType.LANGGRAPH),
        "demo_agent",
    )

    patch_index = entrypoint.index("apply_langchain_patch()")
    load_index = entrypoint.index("runner.load_agent()")
    assert patch_index < load_index
