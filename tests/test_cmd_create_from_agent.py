from pathlib import Path
import asyncio
import importlib
import py_compile
import sys

from click.testing import CliRunner

from ksadk.cli import cmd_create
from ksadk.cli.cmd_deploy import _resolve_artifact_type_input


def test_quick_start_command_lines_quote_posix_project_paths():
    lines = cmd_create._quick_start_command_lines(
        "Demo Agent",
        ["agentengine config"],
        system="Linux",
    )

    assert lines == ["cd 'Demo Agent' && agentengine config"]


def test_quick_start_command_lines_support_windows_powershell_and_cmd():
    lines = cmd_create._quick_start_command_lines(
        "Demo Agent",
        ["agentengine config"],
        system="Windows",
    )

    assert lines == [
        "PowerShell:",
        "Set-Location -LiteralPath 'Demo Agent'",
        "agentengine config",
        "cmd.exe:",
        'cd /d "Demo Agent" && agentengine config',
    ]


def test_find_entry_file_from_agentengine_yaml(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    entry = src / "agentengine_adapter.py"
    entry.write_text("root_agent = object()\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text(
        "framework: langgraph\nentry_point: src/agentengine_adapter.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )

    found = cmd_create._find_entry_file(tmp_path)
    assert found is not None
    found_file, found_var = found
    assert found_file == entry
    assert found_var == "root_agent"


def test_find_entry_file_ignores_config_when_agent_variable_missing(tmp_path: Path):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n",
        encoding="utf-8",
    )
    entry = src / "agent.py"
    entry.write_text(
        "from google.adk.agents import Agent\n"
        "root_agent = Agent(name='demo')\n",
        encoding="utf-8",
    )
    (tmp_path / "agentengine.yaml").write_text(
        "framework: adk\nentry_point: src/demo/main.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )

    found = cmd_create._find_entry_file(tmp_path)

    assert found is not None
    found_file, found_var = found
    assert found_file == entry
    assert found_var == "root_agent"


def test_find_entry_file_prefers_valid_langgraph_json(tmp_path: Path):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    entry = src / "graph.py"
    entry.write_text(
        "from deepagents import create_deep_agent\n"
        "graph = create_deep_agent(model=None)\n",
        encoding="utf-8",
    )
    (tmp_path / "agentengine.yaml").write_text(
        "framework: deepagents\nentry_point: src/demo/main.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )
    (tmp_path / "langgraph.json").write_text(
        '{"graphs": {"agent": "./src/demo/graph.py:graph"}}\n',
        encoding="utf-8",
    )

    found = cmd_create._find_entry_file(tmp_path)

    assert found is not None
    found_file, found_var = found
    assert found_file == entry
    assert found_var == "graph"


def test_find_entry_file_ignores_langgraph_json_local_variable(tmp_path: Path):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    graph_file = src / "graph.py"
    graph_file.write_text(
        "from deepagents import create_deep_agent\n"
        "async def init_agent_resources():\n"
        "    graph = create_deep_agent(model=None)\n"
        "    return graph\n",
        encoding="utf-8",
    )
    adapter = src / "agentengine_adapter.py"
    adapter.write_text("root_agent = object()\n", encoding="utf-8")
    (tmp_path / "langgraph.json").write_text(
        '{"graphs": {"agent": "./src/demo/graph.py:graph"}}\n',
        encoding="utf-8",
    )

    found = cmd_create._find_entry_file(tmp_path)

    assert found is not None
    found_file, found_var = found
    assert found_file == adapter
    assert found_var == "root_agent"


def test_find_entry_file_recursive_scan(tmp_path: Path):
    entry = tmp_path / "src" / "nested" / "custom_entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("root_agent = object()\n", encoding="utf-8")

    found = cmd_create._find_entry_file(tmp_path)
    assert found is not None
    found_file, found_var = found
    assert found_file == entry
    assert found_var == "root_agent"


def test_wrap_agent_directory_ignores_venv_and_exports_nested_entry(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    entry = source / "src" / "agentengine_adapter.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "def build_agent():\n"
        "    return {\"ok\": True}\n"
        "root_agent = build_agent()\n",
        encoding="utf-8",
    )

    # Should be excluded by copytree ignore rules
    venv_file = source / ".venv-ae" / "lib" / "dummy.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-project"
    cmd_create._wrap_agent_directory(source, str(project_path), "langgraph", entry, "root_agent")

    package_dir = project_path / "wrapped_project"
    assert package_dir.exists()
    assert not (package_dir / ".venv-ae").exists()

    init_content = (package_dir / "__init__.py").read_text(encoding="utf-8")
    assert "from .src.agentengine_adapter import root_agent as root_agent" in init_content


def test_wrap_langgraph_messages_directory_does_not_generate_adapter(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    entry = source / "agent.py"
    source.mkdir()
    entry.write_text(
        "from langgraph.graph import MessagesState\n"
        "def node(state):\n"
        "    return {\"messages\": []}\n"
        "root_agent = object()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-messages"
    cmd_create._wrap_agent_directory(source, str(project_path), "langgraph", entry, "root_agent")

    package_dir = project_path / "wrapped_messages"
    assert not (package_dir / "agentengine_adapter.py").exists()
    config_text = (project_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "entry_point: wrapped_messages/agent.py" in config_text


def test_wrap_langgraph_custom_state_directory_generates_adapter(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    entry = source / "agent.py"
    source.mkdir()
    entry.write_text(
        "from typing import TypedDict\n"
        "class State(TypedDict):\n"
        "    query: str\n"
        "def node(state: State):\n"
        "    return {\"answer\": state[\"query\"]}\n"
        "workflow = 'StateGraph(State)'\n"
        "root_agent = object()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-custom"
    cmd_create._wrap_agent_directory(source, str(project_path), "langgraph", entry, "root_agent")

    package_dir = project_path / "wrapped_custom"
    adapter_text = (package_dir / "agentengine_adapter.py").read_text(encoding="utf-8")
    assert "from .agent import root_agent as root_agent" in adapter_text
    assert '"query": payload.get("input", "")' in adapter_text
    config_text = (project_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "entry_point: wrapped_custom/agentengine_adapter.py" in config_text
    assert "agent_variable: root_agent" in config_text


def test_wrap_langgraph_custom_state_directory_detects_state_outside_entry(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("from .graph import root_agent\n", encoding="utf-8")
    (source / "graph.py").write_text(
        "from typing import TypedDict\n"
        "from langgraph.graph import StateGraph\n"
        "class State(TypedDict):\n"
        "    question: str\n"
        "def node(state: State):\n"
        "    return {\"answer\": state[\"question\"]}\n"
        "root_agent = object()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-split-custom"
    cmd_create._wrap_agent_directory(source, str(project_path), "langgraph", source / "agent.py", "root_agent")

    package_dir = project_path / "wrapped_split_custom"
    adapter_text = (package_dir / "agentengine_adapter.py").read_text(encoding="utf-8")
    assert '"question": payload.get("input", "")' in adapter_text
    config_text = (project_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "entry_point: wrapped_split_custom/agentengine_adapter.py" in config_text


def test_wrap_langgraph_ambiguous_file_generates_review_adapter(tmp_path: Path, monkeypatch):
    source = tmp_path / "agent.py"
    source.write_text(
        "from langgraph.graph import StateGraph\n"
        "root_agent = object()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-ambiguous"
    cmd_create._wrap_agent_file(source, str(project_path), "langgraph", "root_agent")

    package_dir = project_path / "wrapped_ambiguous"
    adapter_text = (package_dir / "agentengine_adapter.py").read_text(encoding="utf-8")
    assert "TODO: Map AgentEngine's chat payload" in adapter_text
    assert "return dict(payload)" in adapter_text
    config_text = (project_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "entry_point: wrapped_ambiguous/agentengine_adapter.py" in config_text


def test_wrap_deepagents_service_directory_generates_runtime_adapter(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    pkg = source / "src" / "bill_diagnosis"
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from .lifespan import lifespan\n"
        "app = FastAPI(lifespan=lifespan)\n",
        encoding="utf-8",
    )
    (pkg / "graph.py").write_text(
        "from deepagents import create_deep_agent\n"
        "async def init_agent_resources():\n"
        "    return create_deep_agent(model=None), None, None, None\n",
        encoding="utf-8",
    )
    (pkg / "lifespan.py").write_text(
        "class DeepAgentRunnable:\n"
        "    def __init__(self, agent, langfuse_mgr=None):\n"
        "        self.agent = agent\n"
        "    async def _ainvoke(self, input, config=None, **kwargs):\n"
        "        return {\"response\": input.get(\"message\", \"\")}\n",
        encoding="utf-8",
    )
    (source / "agentengine.yaml").write_text(
        "framework: deepagents\nentry_point: src/bill_diagnosis/main.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-service"
    cmd_create._wrap_agent_directory(source, str(project_path), "deepagents", source / "src" / "bill_diagnosis" / "main.py", "root_agent")

    package_dir = project_path / "wrapped_service"
    adapter_text = (package_dir / "agentengine_adapter.py").read_text(encoding="utf-8")
    assert "class AgentEngineDeepAgentsServiceAdapter" in adapter_text
    assert "async def ainvoke" in adapter_text
    assert '"message": message' in adapter_text
    assert 'INIT_MODULE = ".src.bill_diagnosis.graph"' in adapter_text
    assert "importlib.import_module(INIT_MODULE, __package__)" in adapter_text
    config_text = (project_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "entry_point: wrapped_service/agentengine_adapter.py" in config_text
    assert "agent_variable: root_agent" in config_text


def test_wrap_deepagents_service_directory_ignores_langgraph_json_local_graph(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    pkg = source / "src" / "bill_diagnosis"
    pkg.mkdir(parents=True)
    graph_file = pkg / "graph.py"
    graph_file.write_text(
        "from deepagents import create_deep_agent\n"
        "async def init_agent_resources():\n"
        "    graph = create_deep_agent(model=None)\n"
        "    return graph, None, None, None\n",
        encoding="utf-8",
    )
    (pkg / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from .lifespan import lifespan\n"
        "app = FastAPI(lifespan=lifespan)\n",
        encoding="utf-8",
    )
    (pkg / "lifespan.py").write_text(
        "class DeepAgentRunnable:\n"
        "    async def _ainvoke(self, input, config=None, **kwargs):\n"
        "        return {\"response\": input.get(\"message\", \"\")}\n",
        encoding="utf-8",
    )
    (source / "langgraph.json").write_text(
        '{"graphs": {"agent": "./src/bill_diagnosis/graph.py:graph"}}\n',
        encoding="utf-8",
    )
    (source / "agentengine.yaml").write_text(
        "framework: deepagents\n"
        "entry_point: src/bill_diagnosis/graph.py\n"
        "agent_variable: graph\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    found = cmd_create._find_entry_file(source)
    assert found is not None
    found_file, found_var = found
    assert found_file == graph_file
    assert found_var == "root_agent"

    project_path = tmp_path / "wrapped-service-local-graph"
    cmd_create._wrap_agent_directory(source, str(project_path), "deepagents", found_file, found_var)

    package_dir = project_path / "wrapped_service_local_graph"
    config_text = (project_path / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "entry_point: wrapped_service_local_graph/agentengine_adapter.py" in config_text
    assert "agent_variable: root_agent" in config_text
    adapter_text = (package_dir / "agentengine_adapter.py").read_text(encoding="utf-8")
    assert 'INIT_MODULE = ".src.bill_diagnosis.graph"' in adapter_text
    assert not (package_dir / "ksadk_agentengine_adapter.py").exists()


def test_generated_deepagents_service_adapter_invokes_fake_service(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    pkg = source / "src" / "bill_diagnosis"
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from .lifespan import lifespan\n"
        "app = FastAPI(lifespan=lifespan)\n",
        encoding="utf-8",
    )
    (pkg / "graph.py").write_text(
        "# deepagents create_deep_agent(\n"
        "class FakeGraph:\n"
        "    async def ainvoke(self, payload, **kwargs):\n"
        "        return {\"messages\": [{\"content\": payload.get(\"message\", \"\")}]}\n"
        "async def init_agent_resources():\n"
        "    return FakeGraph(), None, None, None\n",
        encoding="utf-8",
    )
    (pkg / "lifespan.py").write_text(
        "class DeepAgentRunnable:\n"
        "    def __init__(self, agent, langfuse_mgr=None):\n"
        "        self.agent = agent\n"
        "    async def _ainvoke(self, input, config=None, **kwargs):\n"
        "        return {\"response\": \"service:\" + input.get(\"message\", \"\")}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: False)
    monkeypatch.setattr("ksadk.configs.global_config.get_env_from_global_config", lambda: {})

    project_path = tmp_path / "wrapped-service"
    cmd_create._wrap_agent_directory(source, str(project_path), "deepagents", source / "src" / "bill_diagnosis" / "main.py", "root_agent")

    sys.path.insert(0, str(project_path))
    try:
        module = importlib.import_module("wrapped_service.agentengine_adapter")
        result = asyncio.run(module.root_agent.ainvoke({"input": "hello", "session_id": "s1"}))
    finally:
        sys.path.remove(str(project_path))

    assert result["output"] == "service:hello"


def test_create_openclaw_only_generates_env_file(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: True)
    monkeypatch.setattr(
        "ksadk.configs.global_config.get_env_from_global_config",
        lambda: {
            "OPENAI_API_KEY": "sk-openclaw",
            "OPENAI_BASE_URL": "https://model.example.com/v1",
            "OPENAI_MODEL_NAME": "glm-5.1",
            "LANGFUSE_PUBLIC_KEY": "pk-should-not-exist",
            "LANGFUSE_SECRET_KEY": "sk-should-not-exist",
            "LANGFUSE_BASE_URL": "https://langfuse.example.com",
            "KSYUN_ACCESS_KEY": "ak-demo",
            "KSYUN_SECRET_KEY": "sk-demo",
            "KSYUN_REGION": "cn-beijing-6",
            "KSYUN_ACCOUNT_ID": "1234567890",
        },
    )

    result = runner.invoke(cmd_create.create, ["demo-openclaw", "-f", "openclaw"])

    assert result.exit_code == 0, result.output

    project_dir = tmp_path / "demo-openclaw"
    assert project_dir.exists()
    assert sorted(path.name for path in project_dir.iterdir()) == [".env"]

    env_text = (project_dir / ".env").read_text(encoding="utf-8-sig")
    assert "KSYUN_ACCESS_KEY=ak-demo" in env_text
    assert "KSYUN_SECRET_KEY=sk-demo" in env_text
    assert "KSYUN_REGION=cn-beijing-6" in env_text
    assert "KSYUN_ACCOUNT_ID=1234567890" in env_text
    assert "OPENAI_API_KEY=sk-openclaw" in env_text
    assert "OPENAI_BASE_URL=https://model.example.com/v1" in env_text
    assert "OPENAI_MODEL_NAME=glm-5.1" in env_text
    assert "LANGFUSE_" not in env_text


def test_create_hermes_generates_container_first_template(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: True)
    monkeypatch.setattr(
        "ksadk.configs.global_config.get_env_from_global_config",
        lambda: {
            "OPENAI_API_KEY": "sk-hermes",
            "OPENAI_BASE_URL": "https://model.example.com/v1",
            "OPENAI_MODEL_NAME": "glm-hermes",
            "KSYUN_ACCESS_KEY": "ak-demo",
            "KSYUN_SECRET_KEY": "sk-demo",
            "KSYUN_REGION": "cn-beijing-6",
        },
    )

    result = runner.invoke(cmd_create.create, ["demo-hermes", "-f", "hermes"])

    assert result.exit_code == 0, result.output
    project_dir = tmp_path / "demo-hermes"
    assert (project_dir / ".env").exists()
    assert (project_dir / ".env.example").exists()
    assert (project_dir / "agentengine.yaml").exists()
    assert (project_dir / "Dockerfile").exists()
    assert (project_dir / "entrypoint.sh").exists()
    assert (project_dir / "runtime" / "app.py").exists()
    assert (project_dir / "README.md").exists()
    assert not (project_dir / "demo_hermes" / "agent.py").exists()

    config_text = (project_dir / "agentengine.yaml").read_text(encoding="utf-8-sig")
    assert "framework: hermes" in config_text
    assert "artifact_type: Container" in config_text
    assert "ui_profile: hermes" in config_text

    readme_text = (project_dir / "README.md").read_text(encoding="utf-8-sig")
    assert "agentengine hermes deploy" in readme_text
    assert "agentengine launch . --artifact-type Container" not in readme_text

    env_text = (project_dir / ".env").read_text(encoding="utf-8-sig")
    assert "OPENAI_API_KEY=sk-hermes" in env_text
    assert "OPENAI_BASE_URL=https://model.example.com/v1" in env_text
    assert "OPENAI_MODEL_NAME=glm-hermes" in env_text
    py_compile.compile(str(project_dir / "runtime" / "app.py"), doraise=True)


def test_deploy_artifact_type_defaults_to_config_for_hermes_template():
    assert _resolve_artifact_type_input({"artifact_type": "Container"}, None) == "Container"
    assert _resolve_artifact_type_input({"artifact_type": "Container"}, "Code") == "Code"
