from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.sandbox import (
    E2BSandboxBackend,
    SandboxCommandResult,
    SandboxError,
    SandboxInputFile,
    SandboxSpec,
    SandboxType,
    create_sandbox_backend,
)


def test_sandbox_factory_creates_e2b_backend(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")

    backend = create_sandbox_backend(sandbox_cls=object)

    assert isinstance(backend, E2BSandboxBackend)
    assert backend.spec.template_id == "tpl-aio"


def test_sandbox_factory_supports_runtime_template_alias(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.delenv("KSADK_SANDBOX_TEMPLATE_ID", raising=False)
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID", "tpl-skill")

    backend = create_sandbox_backend(sandbox_cls=object)

    assert isinstance(backend, E2BSandboxBackend)
    assert backend.spec.template_id == "tpl-skill"


def test_e2b_sandbox_backend_create_write_run_and_kill(tmp_path: Path):
    calls: list[tuple[str, object]] = []
    source = tmp_path / "input.txt"
    source.write_text("hello", encoding="utf-8")

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
            calls.append(("run_kwargs", kwargs))
            return FakeResult()

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

    backend = E2BSandboxBackend(
        spec=SandboxSpec(
            template_id="tpl-aio",
            sandbox_type=SandboxType.AIO,
            timeout=123,
            allow_internet_access=True,
            metadata={"purpose": "test"},
            env={"BASE_ENV": "1"},
        ),
        sandbox_cls=FakeSandbox,
    )

    session = backend.create_session(
        session_id="sess-1",
        env={"REQUEST_ENV": "2"},
        input_files=[SandboxInputFile(source=source, target_path="/tmp/input.txt")],
    )
    result = session.run_command("python -V", timeout=30, env={"REQUEST_ENV": "command"})
    session.kill()

    assert result == SandboxCommandResult(stdout="ok\n", stderr="", exit_code=0)
    assert calls[0] == (
        "create",
        {
            "template": "tpl-aio",
            "timeout": 123,
            "metadata": {
                "runtime": "ksadk",
                "sandbox_type": "aio",
                "purpose": "test",
                "session_id": "sess-1",
            },
            "envs": {"BASE_ENV": "1", "REQUEST_ENV": "2"},
            "allow_internet_access": True,
        },
    )
    assert ("file_write", ("/tmp/input.txt", b"hello")) in calls
    assert ("run", "python -V") in calls
    assert ("run_kwargs", {"timeout": 30, "envs": {"REQUEST_ENV": "command"}}) in calls
    assert calls[-1] == ("kill", "sbx-123")


def test_e2b_sandbox_backend_waits_for_startup_command_readiness(monkeypatch):
    monkeypatch.setattr("ksadk.sandbox.backends.e2b.time.sleep", lambda _seconds: None)
    calls: list[str] = []

    class NotFoundException(Exception):
        pass

    class FakeResult:
        stdout = "ready\n"
        stderr = ""
        exit_code = 0

    class FakeCommands:
        def run(self, command: str, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                raise NotFoundException()
            return FakeResult()

    class FakeFiles:
        def write(self, path: str, data: str | bytes):
            pass

    class FakeSandbox:
        sandbox_id = "sbx-123"
        def __init__(self):
            self.commands = FakeCommands()
            self.files = FakeFiles()

        @classmethod
        def create(cls, **kwargs):
            return cls()

    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="tpl-aio"),
        sandbox_cls=FakeSandbox,
    )
    e2b_session = backend.create_session(session_id="sess-1")

    result = e2b_session.run_command("python -V")

    assert result == SandboxCommandResult(stdout="ready\n", stderr="", exit_code=0)
    assert calls == ["true", "true", "python -V"]


def test_e2b_sandbox_backend_waits_for_startup_filesystem_readiness(monkeypatch):
    monkeypatch.setattr("ksadk.sandbox.backends.e2b.time.sleep", lambda _seconds: None)
    calls: list[tuple[str, str | bytes]] = []

    class FileNotFoundException(Exception):
        pass

    class FakeResult:
        stdout = ""
        stderr = ""
        exit_code = 0

    class FakeCommands:
        def run(self, command: str, **kwargs):
            return FakeResult()

    class FakeFiles:
        def write(self, path: str, data: str | bytes):
            calls.append((path, data))
            if len(calls) == 1:
                raise FileNotFoundException()

    class FakeSandbox:
        sandbox_id = "sbx-123"
        def __init__(self):
            self.commands = FakeCommands()
            self.files = FakeFiles()

        @classmethod
        def create(cls, **kwargs):
            return cls()

    source = Path(__file__)
    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="tpl-aio"),
        sandbox_cls=FakeSandbox,
    )
    backend.create_session(
        session_id="sess-1",
        input_files=[SandboxInputFile(source=source, target_path="/tmp/input.txt")],
    )

    assert calls[0] == ("/tmp/.ksadk-sandbox-ready", "")
    assert calls[1] == ("/tmp/.ksadk-sandbox-ready", "")
    assert calls[2][0] == "/tmp/input.txt"


def test_e2b_sandbox_backend_requires_template_id():
    with pytest.raises(SandboxError, match="template id"):
        E2BSandboxBackend(spec=SandboxSpec(template_id=""))


def test_sandbox_type_parses_console_types():
    assert SandboxType.from_value("All-in-one") is SandboxType.AIO
    assert SandboxType.from_value("CodeInterpreter") is SandboxType.CODE
    assert SandboxType.from_value("Browser") is SandboxType.BROWSER
    assert SandboxType.from_value("Private") is SandboxType.PRIVATE
