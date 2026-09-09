from ksadk.builders.code_builder import CodeBuilder


def test_bundled_runtime_requirements_include_kingsoftcloud_sdk():
    assert "kingsoftcloud-sdk-python>=1.5.8.94" in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS


def test_bundled_runtime_requirements_include_python_multipart():
    assert "python-multipart>=0.0.9,<1.0.0" in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS


def test_bundled_runtime_requirements_keep_asyncpg_postgres_sessions_optional():
    assert "asyncpg>=0.30.0,<1.0.0" not in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS
    assert "asyncpg>=0.30.0,<1.0.0" in CodeBuilder.BUNDLED_KSADK_POSTGRES_SESSION_REQUIREMENTS
    assert "greenlet>=1.0.0" in CodeBuilder.BUNDLED_KSADK_POSTGRES_SESSION_REQUIREMENTS


def test_bundled_langgraph_postgres_requirements_include_saver_and_pool():
    requirements = CodeBuilder.BUNDLED_KSADK_LANGGRAPH_POSTGRES_REQUIREMENTS
    assert "langgraph-checkpoint-postgres>=3.1.0" in requirements
    assert "psycopg[binary]>=3.2,<4.0" in requirements
    assert "psycopg-pool>=3.2,<4.0" in requirements


def test_bundled_runtime_requirements_keep_mcp_adapters_optional():
    assert "mcp>=1.1.0" not in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS
    assert "langchain-mcp-adapters>=0.0.1" not in CodeBuilder.BUNDLED_KSADK_RUNTIME_REQUIREMENTS
    assert "mcp>=1.1.0" in CodeBuilder.BUNDLED_KSADK_MCP_RUNTIME_REQUIREMENTS
    assert "langchain-mcp-adapters>=0.0.1" in CodeBuilder.BUNDLED_KSADK_MCP_RUNTIME_REQUIREMENTS
