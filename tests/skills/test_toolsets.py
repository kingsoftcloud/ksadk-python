from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx


def _tool_names(tools):
    return [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]


def _zip_bytes(skill_name: str = "demo-skill") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Demo skill\n---\n# Demo\nUse carefully.\n",
        )
    return buf.getvalue()


def test_get_skill_tools_returns_list_load_and_execute_tools(monkeypatch):
    from ksadk.toolsets import get_skill_tools

    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")

    tools = get_skill_tools()

    assert _tool_names(tools) == ["list_skills", "search_skills", "load_skill", "execute_skills"]


def test_search_skills_matches_description_and_metadata(monkeypatch):
    from ksadk.skills.models import SkillListResponse
    from ksadk.toolsets.skills import search_skills

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_skills_by_space_id(self, space_id):
            return SkillListResponse.from_payload(
                {
                    "Data": {
                        "SkillSpaceId": space_id,
                        "Skills": [
                            {
                                "SkillId": "sk-report",
                                "VersionId": "v1",
                                "Version": "1",
                                "Name": "report-writer",
                                "Description": "Write research reports",
                                "Aliases": ["研究报告"],
                                "Tags": ["research"],
                            }
                        ],
                    }
                },
                space_id=space_id,
            )

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setattr("ksadk.toolsets.skills.SkillServiceClient", FakeClient)

    result = search_skills("帮我生成研究报告")

    assert result["ok"] is True
    assert result["results"][0]["name"] == "report-writer"
    assert result["results"][0]["score"] > 0


def test_load_skill_downloads_and_returns_skill_instructions(monkeypatch, tmp_path: Path):
    from ksadk.toolsets.skills import load_skill

    archive = _zip_bytes("demo-skill")
    digest = hashlib.sha256(archive).hexdigest()
    httpx_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SkillSpaceId": "ss-1",
                        "Skills": [
                            {
                                "SkillId": "sk-demo",
                                "VersionId": "sv-demo-v1",
                                "Version": "v1",
                                "Name": "demo-skill",
                                "Description": "Demo skill",
                                "Status": "Active",
                                "ContentHash": f"sha256:{digest}",
                            }
                        ],
                    }
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            return httpx.Response(200, json={"Data": {"DownloadUrl": "https://download.example/demo.zip"}})
        if str(request.url) == "https://download.example/demo.zip":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "https://skill.example/api/v1")
    monkeypatch.setenv("KSADK_SKILL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "ksadk.skills.service_client.httpx.Client",
        lambda **kwargs: httpx_client(
            transport=httpx.MockTransport(handler),
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )

    result = load_skill("demo-skill")

    assert result["ok"] is True
    assert result["name"] == "demo-skill"
    assert result["description"] == "Demo skill"
    assert "Use carefully." in result["instructions"]
    assert result["root_dir"].endswith("demo-skill")


def test_get_agentengine_tools_filters_and_dedupes_groups(monkeypatch):
    from ksadk.toolsets import get_agentengine_tools

    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")

    tools = get_agentengine_tools(include=["skill", "workspace", "skill"])
    names = _tool_names(tools)

    assert names.count("execute_skills") == 1
    assert "list_skills" in names
    assert "search_skills" in names
    assert "workspace_status" in names


def test_platform_toolset_reports_component_status(monkeypatch):
    from ksadk.toolsets import get_platform_tools

    monkeypatch.setenv("OPENAI_MODEL_NAME", "qwen3.6-plus")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-1")

    tools = get_platform_tools()
    names = _tool_names(tools)

    assert "component_status" in names
    component_status = next(tool for tool in tools if (getattr(tool, "name", None) or getattr(tool, "__name__", "")) == "component_status")
    result = component_status.invoke({}) if hasattr(component_status, "invoke") else component_status()
    assert result["summary"]["model"] == "qwen3.6-plus"
    assert result["skill_space"]["space_ids"] == ["ss-1"]
    assert result["sandbox"]["backend"] == "e2b"


def test_sandbox_toolset_reports_sandbox_status(monkeypatch):
    from ksadk.toolsets import get_sandbox_tools

    monkeypatch.delenv("KSADK_SANDBOX_BACKEND", raising=False)
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-1")
    monkeypatch.setenv("KSADK_SANDBOX_TIMEOUT", "321")

    tools = get_sandbox_tools()
    names = _tool_names(tools)

    assert names == ["sandbox_status", "run_command", "run_code"]
    sandbox_status = tools[0]
    result = sandbox_status.invoke({}) if hasattr(sandbox_status, "invoke") else sandbox_status()
    assert result["backend"] == "e2b"
    assert result["template_bound"] is True
    assert result["timeout_seconds"] == 321


def test_describe_agentengine_tools_includes_sandbox_direct_tools(monkeypatch):
    from ksadk.toolsets import describe_agentengine_tools

    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-1")
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")

    tools = describe_agentengine_tools(include=["sandbox"])

    assert [tool["name"] for tool in tools] == ["sandbox_status", "run_command", "run_code"]
    run_command_spec = next(tool for tool in tools if tool["name"] == "run_command")
    assert run_command_spec == {
        "name": "run_command",
        "group": "sandbox",
        "description": "Run a shell command inside the configured isolated sandbox.",
        "risk_level": "high",
        "requires_approval": True,
        "side_effects": ["sandbox_command_execution"],
        "enabled": True,
        "backend": "e2b",
        "boundary": "isolated_sandbox",
    }
    run_code_spec = next(tool for tool in tools if tool["name"] == "run_code")
    assert run_code_spec["risk_level"] == "high"
    assert run_code_spec["requires_approval"] is True
    assert run_code_spec["side_effects"] == ["sandbox_code_execution"]


def test_run_command_executes_through_sandbox_backend(monkeypatch):
    from ksadk.sandbox import SandboxCommandResult
    from ksadk.toolsets.sandbox import run_command

    calls: list[tuple[str, object]] = []

    class FakeSession:
        sandbox_id = "sbx-1"

        def run_command(self, command, *, timeout=None, env=None):
            calls.append(("run", {"command": command, "timeout": timeout, "env": env}))
            return SandboxCommandResult(stdout="ok\n", stderr="", exit_code=0)

        def kill(self):
            calls.append(("kill", self.sandbox_id))

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            calls.append(("create", {"session_id": session_id, "env": env, "input_files": input_files}))
            return FakeSession()

    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())

    result = run_command("python -V", timeout=12, env={"A": "1"})

    assert result == {
        "ok": True,
        "backend": "sandbox/e2b",
        "sandbox_id": "sbx-1",
        "command": "python -V",
        "stdout": "ok\n",
        "stderr": "",
        "exit_code": 0,
    }
    assert calls[0][0] == "create"
    assert calls[1] == ("run", {"command": "python -V", "timeout": 12, "env": {"A": "1"}})
    assert calls[-1] == ("kill", "sbx-1")


def test_run_command_requires_approval_when_tool_gateway_is_strict(monkeypatch):
    from ksadk.toolsets.sandbox import run_command

    called = False

    def fake_backend():
        nonlocal called
        called = True

    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", fake_backend)

    result = run_command("rm -rf /tmp/demo")

    assert called is False
    assert result["ok"] is False
    assert result["type"] == "approval_required"
    assert result["approval_request"]["tool_name"] == "run_command"
    assert result["approval_request"]["risk_level"] == "high"


def test_run_code_writes_file_and_executes_in_sandbox(monkeypatch):
    from ksadk.sandbox import SandboxCommandResult
    from ksadk.toolsets.sandbox import run_code

    calls: list[tuple[str, object]] = []

    class FakeSession:
        sandbox_id = "sbx-code"

        def write_file(self, path, data):
            calls.append(("write_file", {"path": path, "data": data}))

        def run_command(self, command, *, timeout=None, env=None):
            calls.append(("run", {"command": command, "timeout": timeout, "env": env}))
            return SandboxCommandResult(stdout="42\n", stderr="", exit_code=0)

        def kill(self):
            calls.append(("kill", self.sandbox_id))

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            calls.append(("create", {"session_id": session_id, "env": env, "input_files": input_files}))
            return FakeSession()

    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())

    result = run_code("print(42)", language="python", timeout=9)

    assert result["ok"] is True
    assert result["stdout"] == "42\n"
    assert result["language"] == "python"
    write_call = next(call for call in calls if call[0] == "write_file")
    assert write_call[1]["path"].startswith("/tmp/ksadk-run-code-")
    assert write_call[1]["path"].endswith(".py")
    assert write_call[1]["data"] == "print(42)"
    run_call = next(call for call in calls if call[0] == "run")
    assert run_call[1]["command"].startswith("python ")
    assert run_call[1]["timeout"] == 9


def test_workspace_tools_constrain_paths_to_workspace_root(monkeypatch, tmp_path: Path):
    from ksadk.toolsets.workspace import read_workspace_file, resolve_workspace_path, write_workspace_file

    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))

    result = write_workspace_file("notes/demo.txt", "hello")

    assert result["ok"] is True
    assert result["path"] == "notes/demo.txt"
    assert read_workspace_file("notes/demo.txt")["content"] == "hello"
    try:
        resolve_workspace_path("../outside.txt")
    except ValueError as exc:
        assert "workspace root" in str(exc)
    else:
        raise AssertionError("expected workspace path traversal to be rejected")


def test_workspace_write_requires_approval_when_tool_gateway_is_strict(monkeypatch, tmp_path: Path):
    from ksadk.toolsets.workspace import write_workspace_file

    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")

    result = write_workspace_file("notes/demo.txt", "hello")

    assert result["ok"] is False
    assert result["type"] == "approval_required"
    assert result["approval_required"] is True
    assert result["approval_request"]["tool_name"] == "write_workspace_file"
    assert result["approval_request"]["risk_level"] == "medium"
    assert not (tmp_path / "ui" / "workspace" / "notes" / "demo.txt").exists()


def test_workspace_read_still_runs_when_tool_gateway_is_strict(monkeypatch, tmp_path: Path):
    from ksadk.toolsets.workspace import read_workspace_file

    workspace = tmp_path / "ui" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")

    result = read_workspace_file("notes.txt")

    assert result["ok"] is True
    assert result["content"] == "hello"


def test_execute_skills_requires_approval_when_tool_gateway_is_strict(monkeypatch):
    from ksadk.toolsets.skills import execute_skills

    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")

    result = execute_skills("build a web page", skill_names=["web-artifacts-builder"])

    assert result["ok"] is False
    assert result["type"] == "approval_required"
    assert result["approval_request"]["tool_name"] == "execute_skills"
    assert result["approval_request"]["risk_level"] == "high"
