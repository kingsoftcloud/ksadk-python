from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ksadk.skills.events import SkillEvent
from ksadk.skills.runtime import (
    SkillRuntimeError,
    SkillRuntimeResult,
    SkillWorkflowRequest,
    create_skill_runtime_backend,
)
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend


def test_runtime_factory_creates_disabled_backend_by_default(monkeypatch):
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_BACKEND", raising=False)

    backend = create_skill_runtime_backend()

    with pytest.raises(SkillRuntimeError, match="disabled"):
        backend.run_workflow("hello", skill_space_ids=["ss-1"], session_id="s1")


def test_skill_workflow_request_is_public_runtime_protocol():
    request = SkillWorkflowRequest(workflow_prompt="build", skill_names=["demo-skill"])

    assert request.workflow_prompt == "build"
    assert request.skill_names == ["demo-skill"]


def test_runtime_result_only_serializes_skill_events_when_present() -> None:
    empty = SkillRuntimeResult(exit_code=0)
    event = SkillEvent.create(
        "skill.load.completed", status="completed", skill_invocation_id="inv-1"
    )

    assert "skill_events" not in empty.to_dict()
    assert SkillRuntimeResult(exit_code=0, skill_events=[event]).to_dict()["skill_events"] == [
        event.to_dict()
    ]


def test_runtime_factory_creates_local_process_backend(monkeypatch, tmp_path: Path):
    agent = tmp_path / "agent.py"
    agent.write_text("print('agent')", encoding="utf-8")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "local_process")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_AGENT_PATH", str(agent))

    backend = create_skill_runtime_backend()

    assert isinstance(backend, LocalProcessSkillRuntimeBackend)


def test_runtime_factory_auto_uses_e2b_when_generic_sandbox_template_is_configured(monkeypatch):
    sentinel = object()
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_BACKEND", raising=False)
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    monkeypatch.setattr(
        "ksadk.skills.runtime.factory.E2BSkillRuntimeBackend.from_env",
        lambda: sentinel,
    )

    backend = create_skill_runtime_backend()

    assert backend is sentinel


def test_e2b_skill_runtime_backend_from_env_prefers_generic_sandbox_vars(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID", "tpl-legacy")
    monkeypatch.setenv("KSADK_SANDBOX_TIMEOUT", "321")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_TIMEOUT", "123")
    monkeypatch.setenv("KSADK_SANDBOX_ALLOW_INTERNET_ACCESS", "false")
    monkeypatch.setattr("e2b.Sandbox", object)

    backend = E2BSkillRuntimeBackend.from_env()

    assert backend.template_id == "tpl-aio"
    assert backend.timeout == 321
    assert backend.allow_internet_access is False


def test_e2b_skill_runtime_backend_error_mentions_generic_template_var():
    with pytest.raises(SkillRuntimeError, match="KSADK_SANDBOX_TEMPLATE_ID"):
        E2BSkillRuntimeBackend(template_id="")


def test_e2b_backend_uses_native_env_and_always_kills(monkeypatch):
    calls: list[tuple[str, object]] = []

    class FakeResult:
        stdout = 'ok\nworkflow_result={"output_files":["/tmp/bundle.html"],"status":"ok"}\n'
        stderr = ""
        exit_code = 0

    class FakeCommands:
        def run(self, command: str, **kwargs):
            calls.append(("run", command))
            calls.append(("run_kwargs", kwargs))
            return FakeResult()

    class FakeFiles:
        def write(self, path, data):
            calls.append(("file_write", (path, data)))

    class FakeSandbox:
        sandbox_id = "sbx-123"

        def __init__(self):
            self.files = FakeFiles()
            self.commands = FakeCommands()

        @classmethod
        def create(cls, **kwargs):
            calls.append(("create", kwargs))
            return cls()

        def kill(self):
            calls.append(("kill", self.sandbox_id))

    backend = E2BSkillRuntimeBackend(sandbox_cls=FakeSandbox, template_id="tpl-1", timeout=123)

    result = backend.run_workflow(
        "build artifact",
        skill_space_ids=["ss-1"],
        skill_names=["demo-skill"],
        session_id="sess-1",
        env={"TRACEPARENT": "attempted-override"},
    )

    assert result.runtime_id == "sbx-123"
    assert result.exit_code == 0
    assert (
        result.stdout == 'ok\nworkflow_result={"output_files":["/tmp/bundle.html"],"status":"ok"}\n'
    )
    assert result.stderr == ""
    assert result.output_files == ["/tmp/bundle.html"]
    assert [event.event_type for event in result.skill_events] == [
        "sandbox.session.created",
        "sandbox.session.cleaned_up",
    ]
    assert calls[0] == (
        "create",
        {
            "template": "tpl-1",
            "timeout": 123,
            "metadata": {
                "runtime": "ksadk",
                "sandbox_type": "aio",
                "component": "skill-runtime",
                "session_id": "sess-1",
            },
            "envs": {
                "KSADK_SKILL_SPACE_IDS": "ss-1",
                "SKILL_SPACE_ID": "ss-1",
                "KSADK_SELECTED_SKILL_NAMES": "demo-skill",
            },
            "allow_internet_access": True,
        },
    )
    run_kwargs = next(value for name, value in calls if name == "run_kwargs" and "timeout" in value)
    assert run_kwargs["timeout"] == 900
    assert run_kwargs["envs"]["KSADK_SKILL_SPACE_IDS"] == "ss-1"
    assert run_kwargs["envs"]["SKILL_SPACE_ID"] == "ss-1"
    assert run_kwargs["envs"]["KSADK_SELECTED_SKILL_NAMES"] == "demo-skill"
    assert "TRACEPARENT" not in run_kwargs["envs"]
    assert run_kwargs["envs"]["KSADK_SKILL_EVENT_FILE"].startswith("/tmp/ksadk-skill-events-")
    request_write = next(
        value
        for name, value in calls
        if name == "file_write" and value[0] == "/tmp/ksadk-workflow-request.json"
    )
    assert request_write[0] == "/tmp/ksadk-workflow-request.json"
    assert json.loads(request_write[1].decode("utf-8")) == {
        "workflow_prompt": "build artifact",
        "skill_names": ["demo-skill"],
    }
    assert calls[-1] == ("kill", "sbx-123")


def test_e2b_backend_preserves_public_skill_space_env(monkeypatch):
    monkeypatch.setenv("KSADK_PUBLIC_SKILL_SPACE_IDS", "ss-public")
    calls: list[tuple[str, object]] = []

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeCommands:
        def run(self, command: str, **kwargs):
            return FakeResult()

    class FakeFiles:
        def write(self, path, data):
            pass

    class FakeSandbox:
        sandbox_id = "sbx-123"

        def __init__(self):
            self.files = FakeFiles()
            self.commands = FakeCommands()

        @classmethod
        def create(cls, **kwargs):
            calls.append(("create", kwargs))
            return cls()

        def kill(self):
            pass

    backend = E2BSkillRuntimeBackend(sandbox_cls=FakeSandbox, template_id="tpl-1")

    backend.run_workflow("build artifact", skill_space_ids=["ss-user"], session_id="sess-1")

    envs = calls[0][1]["envs"]
    assert envs["KSADK_SKILL_SPACE_IDS"] == "ss-user"
    assert envs["SKILL_SPACE_ID"] == "ss-user"
    assert envs["KSADK_PUBLIC_SKILL_SPACE_IDS"] == "ss-public"


def test_e2b_backend_recovers_skill_event_sidecar():
    event = SkillEvent.create(
        "skill.execution.completed",
        status="completed",
        skill_invocation_id="inv-1",
        runtime_id="sbx-123",
    )

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeFiles:
        def write(self, path, data):
            pass

        def read(self, path):
            assert path.startswith("/tmp/ksadk-skill-events-")
            assert path.endswith(".jsonl")
            return json.dumps(event.to_dict()) + "\n"

    class FakeCommands:
        def run(self, command: str, **kwargs):
            if "/home/ksadk/agent.py" in command:
                assert kwargs["envs"]["KSADK_SKILL_EVENT_FILE"].startswith(
                    "/tmp/ksadk-skill-events-"
                )
            return FakeResult()

    class FakeSandbox:
        sandbox_id = "sbx-123"

        def __init__(self):
            self.files = FakeFiles()
            self.commands = FakeCommands()

        @classmethod
        def create(cls, **kwargs):
            return cls()

        def kill(self):
            pass

    result = E2BSkillRuntimeBackend(sandbox_cls=FakeSandbox, template_id="tpl-1").run_workflow(
        "build", skill_space_ids=["ss-1"], session_id="sess-1"
    )

    assert [item.event_type for item in result.skill_events] == [
        "sandbox.session.created",
        "skill.execution.completed",
        "sandbox.session.cleaned_up",
    ]
    assert result.skill_events[1] == event
    assert result.skill_events[1].runtime_id == "sbx-123"


def test_e2b_backend_reports_cleanup_failure_without_changing_result():
    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeFiles:
        def write(self, path, data):
            pass

    class FakeCommands:
        def run(self, command: str, **kwargs):
            return FakeResult()

    class FakeSandbox:
        sandbox_id = "sbx-123"

        def __init__(self):
            self.files = FakeFiles()
            self.commands = FakeCommands()

        @classmethod
        def create(cls, **kwargs):
            return cls()

        def kill(self):
            raise RuntimeError("cleanup unavailable")

    result = E2BSkillRuntimeBackend(sandbox_cls=FakeSandbox, template_id="tpl-1").run_workflow(
        "build", skill_space_ids=["ss-1"], session_id="sess-1"
    )

    assert result.ok
    assert [item.event_type for item in result.skill_events] == [
        "sandbox.session.created",
        "sandbox.session.cleanup_failed",
    ]
    assert result.skill_events[-1].error_category == "cleanup_failed"


def test_e2b_backend_redacts_secret_from_errors(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "super-secret-token")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_TOKEN", "skill-service-token")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_SECRET_KEY", "skill-service-secret")

    class FakeSandbox:
        @classmethod
        def create(cls, **kwargs):
            raise RuntimeError(
                "failed with super-secret-token and skill-service-token and skill-service-secret"
            )

    backend = E2BSkillRuntimeBackend(sandbox_cls=FakeSandbox, template_id="tpl-1")

    result = backend.run_workflow("x", skill_space_ids=["ss-1"], session_id="sess-1")

    assert result.exit_code is None
    assert result.error_type == "RuntimeError"
    assert "super-secret-token" not in result.error_message
    assert "skill-service-token" not in result.error_message
    assert "skill-service-secret" not in result.error_message
    assert "[REDACTED]" in result.error_message


def test_e2b_backend_writes_request_file_instead_of_shell_quoting_long_prompt():
    calls: list[tuple[str, object]] = []

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeFiles:
        def write(self, path, data):
            calls.append(("file_write", (path, data)))

    class FakeCommands:
        def run(self, command: str, **kwargs):
            calls.append(("run", command))
            return FakeResult()

    class FakeSandbox:
        sandbox_id = "sbx-123"

        def __init__(self):
            self.files = FakeFiles()
            self.commands = FakeCommands()

        @classmethod
        def create(cls, **kwargs):
            return cls()

        def kill(self):
            calls.append(("kill", self.sandbox_id))

    backend = E2BSkillRuntimeBackend(sandbox_cls=FakeSandbox, template_id="tpl-1")

    backend.run_workflow(
        "hello 'quoted'",
        skill_space_ids=["ss-1"],
        skill_names=["demo-skill"],
        session_id="sess-1",
    )

    request_write = next(
        value
        for name, value in calls
        if name == "file_write" and value[0] == "/tmp/ksadk-workflow-request.json"
    )
    request_path, request_bytes = request_write
    assert request_path == "/tmp/ksadk-workflow-request.json"
    assert json.loads(request_bytes.decode("utf-8")) == {
        "workflow_prompt": "hello 'quoted'",
        "skill_names": ["demo-skill"],
    }
    run_command = next(
        value for name, value in calls if name == "run" and "/home/ksadk/agent.py" in value
    )
    assert (
        run_command
        == "python -u /home/ksadk/agent.py --request-file /tmp/ksadk-workflow-request.json"
    )


def test_local_process_backend_writes_request_file_envelope(monkeypatch, tmp_path: Path):
    calls: list[dict[str, object]] = []
    agent = tmp_path / "agent.py"
    agent.write_text("print('agent')", encoding="utf-8")

    def fake_run(args, **kwargs):
        request_path = Path(args[-1])
        calls.append(
            {
                "args": args,
                "request": json.loads(request_path.read_text(encoding="utf-8")),
                "env": kwargs["env"],
            }
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("ksadk.skills.runtime.backends.local.subprocess.run", fake_run)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")
    monkeypatch.setenv("TRACEPARENT", "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "not-for-sandbox")
    backend = LocalProcessSkillRuntimeBackend(agent_path=agent)

    backend.run_workflow(
        "build artifact",
        skill_space_ids=["ss-1"],
        skill_names=["demo-skill"],
        session_id="sess-1",
        env={"OTEL_EXPORTER_OTLP_HEADERS": "attempted-override"},
    )

    assert calls[0]["args"][:3] == [sys.executable, "-u", str(agent)]
    assert calls[0]["args"][3] == "--request-file"
    assert calls[0]["request"] == {
        "workflow_prompt": "build artifact",
        "skill_names": ["demo-skill"],
    }
    assert calls[0]["env"]["KSADK_SELECTED_SKILL_NAMES"] == "demo-skill"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in calls[0]["env"]
    assert "TRACEPARENT" not in calls[0]["env"]
    assert "LANGFUSE_SECRET_KEY" not in calls[0]["env"]
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in calls[0]["env"]


def test_local_process_backend_recovers_skill_event_sidecar(monkeypatch, tmp_path: Path):
    agent = tmp_path / "agent.py"
    agent.write_text("print('agent')", encoding="utf-8")
    event = SkillEvent.create(
        "skill.load.completed", status="completed", skill_invocation_id="inv-1"
    )

    def fake_run(args, **kwargs):
        event_path = Path(kwargs["env"]["KSADK_SKILL_EVENT_FILE"])
        event_path.write_text(json.dumps(event.to_dict()) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("ksadk.skills.runtime.backends.local.subprocess.run", fake_run)

    result = LocalProcessSkillRuntimeBackend(agent_path=agent).run_workflow(
        "build artifact", skill_space_ids=["ss-1"], session_id="sess-1"
    )

    assert [item.event_type for item in result.skill_events] == [
        "sandbox.session.created",
        "skill.load.completed",
        "sandbox.session.cleaned_up",
    ]
    assert result.skill_events[1] == replace(event, runtime_id="local:sess-1")
    assert result.skill_events[1].runtime_id == "local:sess-1"
