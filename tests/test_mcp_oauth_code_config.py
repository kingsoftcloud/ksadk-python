from copy import deepcopy

import pytest

from ksadk.mcp_runtime.oauth.tools import McpOAuthTools

OPTIONS = {
    "url": "https://provider.example/mcp",
    "description": "Cloud resources",
    "scopes": ["read"],
}


def test_object_keeps_a_private_declaration_without_global_registration():
    source = {"cloud": deepcopy(OPTIONS)}
    component = McpOAuthTools(source)
    assert component.servers == {"cloud": "Cloud resources"}
    assert component.declarations[0]["url"] == OPTIONS["url"]
    assert component.declarations[0]["oauth"]["scopes"] == ["read"]
    assert component.declarations[0]["tool_policies"] == {}
    assert source == {"cloud": OPTIONS}
    assert McpOAuthTools({"other": "Existing name"}).declarations == []


@pytest.mark.parametrize(
    "extra",
    [
        {"client_secret": "must-not-appear"},
        {"auth_mode": "api_key"},
        {"headers": {"Authorization": "must-not-appear"}},
    ],
)
def test_object_rejects_inline_secrets_and_non_oauth_options_without_echo(extra):
    with pytest.raises(ValueError) as exc:
        McpOAuthTools({"cloud": {**OPTIONS, **extra}})
    assert "must-not-appear" not in str(exc.value)


def project(tmp_path, source):
    (tmp_path / "agent.py").write_text(source)
    (tmp_path / "agentengine.yaml").write_text(
        "name: fixture\nframework: langgraph\nentry_point: agent.py\nagent_variable: root_agent\n"
    )


@pytest.mark.parametrize("mode", ["code", "container"])
def test_build_entrypoint_never_exports_agent_python(tmp_path, mode):
    import ast
    import json

    from ksadk.builders.code_builder import CodeBuilder
    from ksadk.builders.container_builder import ContainerBuilder
    from ksadk.detection import FrameworkDetector

    project(
        tmp_path,
        """from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
oauth=McpOAuthTools({"cloud":{"url":"https://provider.example/mcp","scopes":["read"]}})
root_agent=object()
""",
    )
    detection = FrameworkDetector(str(tmp_path)).detect()
    code = (
        CodeBuilder(tmp_path)._generate_entrypoint(detection)
        if mode == "code"
        else ContainerBuilder(tmp_path)._generate_entrypoint(detection, "fixture")
    )
    tree = ast.parse(code)
    assignment = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "runtime_build_config" for t in n.targets)
    )
    config = json.loads(ast.literal_eval(assignment.value.args[0]))
    assert "mcp_servers" not in config
    assert "mcp_servers" not in (tmp_path / "agentengine.yaml").read_text()


def test_local_and_managed_composition_do_not_import_agent_for_oauth(tmp_path, monkeypatch):
    from ksadk.cli import runtime_bootstrap
    from ksadk.detection import FrameworkDetector

    project(
        tmp_path,
        """from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
oauth=McpOAuthTools({"cloud":{"url":"https://provider.example/mcp","scopes":["read"]}})
root_agent=object()
""",
    )
    detection = FrameworkDetector(str(tmp_path)).detect()
    monkeypatch.delenv("AGENTENGINE_MANAGED_RUNTIME", raising=False)
    monkeypatch.setattr(runtime_bootstrap, "create_runtime_app", lambda config, *args: config)
    local = runtime_bootstrap.create_runtime_web_app(detection, tmp_path)
    assert local.local_mcp_oauth and "mcp_servers" not in local.launch_context.config
    monkeypatch.setenv("AGENTENGINE_MANAGED_RUNTIME", "1")
    managed = runtime_bootstrap.create_runtime_web_app(detection, tmp_path)
    assert not managed.local_mcp_oauth


def test_node_environment_is_resolved_at_execution_not_at_build(tmp_path, monkeypatch):
    from ksadk.builders.code_builder import CodeBuilder
    from ksadk.detection import FrameworkDetector

    project(
        tmp_path,
        """import os
from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
oauth=McpOAuthTools({"cloud":{"url":os.environ["MCP_TEST_URL"],"scopes":["read"]}})
root_agent=object()
""",
    )
    detection = FrameworkDetector(str(tmp_path)).detect()
    builder = CodeBuilder(tmp_path)
    monkeypatch.setattr(builder, "_iter_bundled_source_files", lambda: [])
    monkeypatch.setenv("MCP_TEST_URL", "https://first.example/mcp")
    first = builder._build_input_fingerprint(detection)
    monkeypatch.setenv("MCP_TEST_URL", "https://second.example/mcp")
    second = builder._build_input_fingerprint(detection)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["file_digests"] == second["file_digests"]


def test_code_only_oauth_keeps_mcp_dependencies_in_both_build_modes(tmp_path):
    from ksadk.builders.code_builder import CodeBuilder
    from ksadk.builders.container_builder import ContainerBuilder
    from ksadk.detection import FrameworkDetector

    project(
        tmp_path,
        """from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
oauth=McpOAuthTools({"cloud":{"url":"https://provider.example/mcp", "scopes":["read"]}})
root_agent=object()
""",
    )
    detection = FrameworkDetector(str(tmp_path)).detect()
    requirements = CodeBuilder(tmp_path)._build_requirements_list(detection)
    assert any(item.startswith("mcp>=") for item in requirements)
    assert any(item.startswith("asyncpg>=") for item in requirements)
    # Container requirements reuse CodeBuilder's merged dependency list.
    text = ContainerBuilder(tmp_path)._generate_requirements(detection)
    assert "mcp>=" in text
    assert "asyncpg>=" in text


def test_uploaded_yaml_resolves_secret_only_for_selected_connection(tmp_path, monkeypatch):
    from ksadk.mcp_runtime.oauth.client import BrokerError
    from ksadk.mcp_runtime.oauth.code_config import project_declaration

    (tmp_path / "agentengine.yaml").write_text(
        "mcp_servers:\n"
        "  - {name: unused, auth_mode: oauth, oauth: invalid}\n"
        "  - name: cloud\n"
        "    auth_mode: oauth\n"
        "    url: https://provider.example/mcp\n"
        "    oauth: {scopes: [read], client_id: app, client_secret_env: FIXTURE_CLIENT_SECRET}\n"
    )
    monkeypatch.setenv("FIXTURE_CLIENT_SECRET", "fixture-value")
    assert project_declaration(tmp_path, "unknown") is None
    assert project_declaration(tmp_path, "cloud")["oauth"]["client_secret"] == "fixture-value"
    monkeypatch.delenv("FIXTURE_CLIENT_SECRET")
    with pytest.raises(BrokerError, match="MCP_CLIENT_SECRET_MISSING"):
        project_declaration(tmp_path, "cloud")
