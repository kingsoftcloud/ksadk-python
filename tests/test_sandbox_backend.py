from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.sandbox import (
    E2BSandboxBackend,
    LocalProcessSandboxBackend,
    SandboxCommandResult,
    SandboxError,
    SandboxInputFile,
    SandboxSpec,
    SandboxType,
    create_sandbox_backend,
)
from ksadk.runtime_context import tool_execution_scope
from ksadk.sandbox.registry import GLOBAL_SANDBOX_REGISTRY, SandboxRegistry
from ksadk.toolsets.sandbox import run_code, run_command, sandbox_status


@pytest.fixture(autouse=True)
def _reset_sandbox_registry(monkeypatch):
    # 禁用后台 sweep 线程,避免测试间相互干扰。
    monkeypatch.setenv("KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS", "0")
    GLOBAL_SANDBOX_REGISTRY.reset_for_tests()
    yield
    GLOBAL_SANDBOX_REGISTRY.reset_for_tests()


def test_sandbox_factory_creates_e2b_backend(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")

    backend = create_sandbox_backend(sandbox_cls=object)

    assert isinstance(backend, E2BSandboxBackend)
    assert backend.spec.template_id == "tpl-aio"


def test_run_code_returns_snippet_runner_boundary_metadata(monkeypatch):
    class FakeSession:
        sandbox_id = "sbx-code"

        def write_file(self, path, data):
            self.path = path
            self.data = data

        def run_command(self, command, timeout=None, env=None, cwd=None):
            return SandboxCommandResult(stdout="42\n", stderr="", exit_code=0)

    class FakeBackend:
        isolated = True

        def create_session(self, **_kwargs):
            return FakeSession()

    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")

    result = run_code("print(42)", language="python")

    assert result["ok"] is True
    assert result["execution_model"] == "snippet_runner"
    assert result["boundary"]
    assert result["sandbox_id"] == "sbx-code"


def test_run_code_requires_isolated_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local_process")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))

    result = run_code("print('local')", language="python")

    assert result["ok"] is False
    assert result["error_type"] == "isolated_sandbox_required"
    assert "requires an isolated sandbox" in result["error_message"]


def test_sandbox_factory_refuses_e2b_without_template(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.delenv("KSADK_SANDBOX_TEMPLATE_ID", raising=False)
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID", raising=False)

    with pytest.raises(SandboxError, match="template id"):
        create_sandbox_backend()


def test_sandbox_factory_creates_local_process_backend_when_explicit(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local_process")

    backend = create_sandbox_backend()

    assert isinstance(backend, LocalProcessSandboxBackend)
    assert backend.isolated is False


def test_sandbox_factory_requires_gate_for_pod_process(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "pod_process")
    monkeypatch.delenv("KSADK_ALLOW_POD_PROCESS_TOOLS", raising=False)

    with pytest.raises(SandboxError, match="KSADK_ALLOW_POD_PROCESS_TOOLS"):
        create_sandbox_backend()


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


def test_local_process_backend_runs_inside_workspace(tmp_path):
    backend = LocalProcessSandboxBackend(workspace_root=tmp_path)
    session = backend.create_session(session_id="sess-1")

    result = session.run_command("pwd && python -c 'print(123)'")

    assert result.exit_code == 0
    assert str(tmp_path) in result.stdout
    assert "123" in result.stdout


def test_local_process_backend_rejects_cwd_escape(tmp_path):
    backend = LocalProcessSandboxBackend(workspace_root=tmp_path)
    session = backend.create_session(session_id="sess-1")

    result = session.run_command("pwd", env={"KSADK_COMMAND_CWD": "/"})

    assert result.exit_code == 126
    assert "cwd must stay inside" in result.stderr


def test_local_process_backend_kills_process_group_on_timeout(tmp_path):
    backend = LocalProcessSandboxBackend(workspace_root=tmp_path)
    session = backend.create_session(session_id="sess-1")

    result = session.run_command("python -c 'import subprocess, time; subprocess.Popen([\"sleep\", \"5\"]); time.sleep(5)'", timeout=1)

    assert result.exit_code == 124
    assert "command timed out" in result.stderr


def test_sandbox_registry_reuses_session_and_sweeps_idle_entries():
    calls: list[str] = []

    class FakeSession:
        sandbox_id = "fake-1"

        def kill(self):
            calls.append("kill")

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            calls.append(f"create:{session_id}")
            return FakeSession()

    registry = SandboxRegistry()
    first, created_first = registry.get_or_create(
        key="run-1",
        backend_name="fake",
        backend=FakeBackend(),
        ttl_seconds=100,
        idle_ttl_seconds=10,
        isolated=True,
        now=100.0,
    )
    second, created_second = registry.get_or_create(
        key="run-1",
        backend_name="fake",
        backend=FakeBackend(),
        ttl_seconds=100,
        idle_ttl_seconds=10,
        isolated=True,
        now=105.0,
    )
    swept = registry.sweep(now=116.0, idle_ttl_seconds=10)

    assert first is second
    assert created_first is True
    assert created_second is False
    assert swept == 1
    assert calls == ["create:run-1", "kill"]


def test_sandbox_registry_quota_reclaims_oldest_entry():
    killed: list[str] = []

    class FakeSession:
        def __init__(self, sandbox_id: str):
            self.sandbox_id = sandbox_id

        def kill(self):
            killed.append(self.sandbox_id)

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            return FakeSession(session_id)

    registry = SandboxRegistry()
    registry.get_or_create(key="old", backend_name="fake", backend=FakeBackend(), ttl_seconds=100, isolated=True, now=100.0, max_sessions=2)
    registry.get_or_create(key="middle", backend_name="fake", backend=FakeBackend(), ttl_seconds=100, isolated=True, now=101.0, max_sessions=2)
    registry.get_or_create(key="new", backend_name="fake", backend=FakeBackend(), ttl_seconds=100, isolated=True, now=102.0, max_sessions=2)

    assert killed == ["old"]
    assert {entry.key for entry in registry.entries()} == {"middle", "new"}


def test_run_command_syncs_workspace_files_to_new_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    workspace = tmp_path / "ui" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("print('synced')\n", encoding="utf-8")

    created_input_files: list[SandboxInputFile] = []

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeSession:
        sandbox_id = "sbx-sync"

        def write_file(self, path, data):
            pass

        def read_file(self, path):
            return ""

        def run_command(self, command, *, timeout=None, env=None, cwd=None):
            return FakeResult()

        def get_host(self, port):
            return "https://example.com"

        def kill(self):
            pass

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            created_input_files.extend(input_files or [])
            return FakeSession()

    from ksadk.sandbox.registry import GLOBAL_SANDBOX_REGISTRY

    GLOBAL_SANDBOX_REGISTRY.clear()
    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())

    result = run_command("python -V")

    assert result["ok"] is True
    assert [(item.source, item.target_path) for item in created_input_files] == [
        (workspace / "app.py", "/workspace/app.py")
    ]


def test_run_command_and_run_code_reuse_context_session_sandbox_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    (tmp_path / "ui" / "workspace").mkdir(parents=True)
    created_session_ids: list[str] = []
    commands: list[str] = []

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeSession:
        sandbox_id = "sbx-shared"

        def write_file(self, path, data):
            pass

        def run_command(self, command, *, timeout=None, env=None, cwd=None):
            commands.append(command)
            return FakeResult()

        def kill(self):
            pass

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            created_session_ids.append(session_id)
            return FakeSession()

    GLOBAL_SANDBOX_REGISTRY.clear()
    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())

    with tool_execution_scope(session_id="sess-1", run_id="run-1", invocation_id="inv-1"):
        command_result = run_command("python -V")
        code_result = run_code("print(42)")

    assert command_result["ok"] is True
    assert code_result["ok"] is True
    assert created_session_ids == ["ksadk-session:sess-1"]
    assert commands[0] == "python -V"
    assert commands[1].startswith("python /tmp/ksadk-run-code-")
    GLOBAL_SANDBOX_REGISTRY.clear()


def test_sandbox_context_key_isolated_by_session_and_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    (tmp_path / "ui" / "workspace").mkdir(parents=True)
    created_session_ids: list[str] = []

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeSession:
        def __init__(self, sandbox_id: str):
            self.sandbox_id = sandbox_id

        def run_command(self, command, *, timeout=None, env=None, cwd=None):
            return FakeResult()

        def kill(self):
            pass

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            created_session_ids.append(session_id)
            return FakeSession(f"sbx-{len(created_session_ids)}")

    GLOBAL_SANDBOX_REGISTRY.clear()
    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())

    with tool_execution_scope(session_id="sess-a"):
        assert run_command("python -V")["ok"] is True
    with tool_execution_scope(session_id="sess-b"):
        assert run_command("python -V")["ok"] is True
    monkeypatch.setenv("KSADK_SANDBOX_SESSION_ID", "manual")
    with tool_execution_scope(session_id="sess-c"):
        assert run_command("python -V")["ok"] is True

    assert created_session_ids == ["ksadk-session:sess-a", "ksadk-session:sess-b", "manual"]
    GLOBAL_SANDBOX_REGISTRY.clear()


def test_sandbox_direct_calls_retain_prefix_fallback_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "e2b")
    monkeypatch.setenv("KSADK_SANDBOX_TEMPLATE_ID", "tpl-aio")
    (tmp_path / "ui" / "workspace").mkdir(parents=True)
    created_session_ids: list[str] = []

    class FakeResult:
        stdout = "ok\n"
        stderr = ""
        exit_code = 0

    class FakeSession:
        sandbox_id = "sbx-direct"

        def write_file(self, path, data):
            pass

        def run_command(self, command, *, timeout=None, env=None, cwd=None):
            return FakeResult()

        def kill(self):
            pass

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            created_session_ids.append(session_id)
            return FakeSession()

    GLOBAL_SANDBOX_REGISTRY.clear()
    monkeypatch.setattr("ksadk.toolsets.sandbox.create_sandbox_backend", lambda: FakeBackend())

    assert run_command("python -V")["ok"] is True
    assert run_code("print(42)")["ok"] is True

    assert created_session_ids == ["ksadk-direct-shared", "ksadk-code-shared"]
    GLOBAL_SANDBOX_REGISTRY.clear()


def test_sandbox_status_reports_idle_ttl_and_quota(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_BACKEND", "local_process")
    monkeypatch.setenv("KSADK_SANDBOX_IDLE_TTL_SECONDS", "12")
    monkeypatch.setenv("KSADK_SANDBOX_MAX_SESSIONS", "3")

    result = sandbox_status()

    assert result["ok"] is True
    assert result["isolated"] is False
    assert result["idle_ttl_seconds"] == 12
    assert result["max_sessions"] == 3


def test_sandbox_registry_clear_is_idempotent():
    # clear() 幂等:多次调用不抛异常(atexit 和 server shutdown 都可能调)。
    GLOBAL_SANDBOX_REGISTRY.clear()
    GLOBAL_SANDBOX_REGISTRY.clear()
    assert GLOBAL_SANDBOX_REGISTRY.entries() == []


def test_sandbox_registry_sweep_thread_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS", "0")
    registry = SandboxRegistry()
    registry._start_sweep_thread()
    assert registry._sweep_thread is None
    registry.reset_for_tests()


def test_sandbox_registry_sweep_thread_starts_when_interval_positive(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS", "1")
    registry = SandboxRegistry()

    class FakeSession:
        sandbox_id = "sweep-1"
        killed = False

        def kill(self):
            self.killed = True

    class FakeBackend:
        def create_session(self, *, session_id, env=None, input_files=None):
            return FakeSession()

    # 首次 get_or_create 创建 entry 后应懒启动后台 sweep 线程。
    registry.get_or_create(
        key="sweep-test",
        backend_name="fake",
        backend=FakeBackend(),
        ttl_seconds=1,
        idle_ttl_seconds=1,
        isolated=True,
        now=0.0,
    )
    assert registry._sweep_thread is not None
    assert registry._sweep_thread.is_alive()
    registry.reset_for_tests()
    assert registry._sweep_thread is None
    assert registry.entries() == []


def test_sandbox_registry_concurrent_get_or_create_does_not_deadlock(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS", "0")
    import threading

    class FakeSession:
        def __init__(self, sid):
            self.sandbox_id = sid
            self.killed = False

        def kill(self):
            self.killed = True

    class FakeBackend:
        def __init__(self):
            self._counter = 0
            self._lock = threading.Lock()

        def create_session(self, *, session_id, env=None, input_files=None):
            with self._lock:
                self._counter += 1
            return FakeSession(f"sbx-{session_id}-{self._counter}")

    registry = SandboxRegistry()
    backend = FakeBackend()
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            registry.get_or_create(
                key=f"concurrent-{idx}",
                backend_name="fake",
                backend=backend,
                ttl_seconds=100,
                isolated=True,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert not errors
    assert len(registry.entries()) == 8
    registry.reset_for_tests()


def test_shutdown_runner_resources_clears_sandbox_registry(monkeypatch):
    import asyncio
    import sys

    import ksadk.server.app  # noqa: F401  触发模块注册到 sys.modules
    app_module = sys.modules["ksadk.server.app"]

    # 用一个带空 close() 的 mock runner,触发完整 shutdown 路径(含 sandbox clear)。
    class FakeRunner:
        async def close(self):
            return None

    monkeypatch.setattr(app_module, "runner", FakeRunner())
    calls: list[bool] = []
    monkeypatch.setattr(
        GLOBAL_SANDBOX_REGISTRY,
        "clear",
        lambda: calls.append(True),
    )

    asyncio.run(app_module._shutdown_runner_resources())

    assert calls == [True]
