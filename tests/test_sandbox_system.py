from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ksadk.sandbox import (
    BaseSandbox,
    ExecutionConfig,
    ExecutionResult,
    ExecutionStatus,
    Language,
    LocalCodeSandbox,
    RemoteCodeSandbox,
    SandboxToolset,
)


@pytest.mark.asyncio
async def test_local_python_execution_success(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute("print(sum(i * i for i in range(4)))")

    assert result.status is ExecutionStatus.SUCCESS
    assert result.stdout == "14\n"
    assert result.stderr == ""
    assert result.metadata["return_code"] == 0


@pytest.mark.asyncio
async def test_local_bash_execution_success(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute(
        "printf 'alpha\\nbeta\\n' | wc -l",
        language=Language.BASH,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.stdout.strip() == "2"


@pytest.mark.asyncio
async def test_local_javascript_execution_success(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute(
        "console.log([1, 2, 3].map((value) => value * 2).join(','));",
        language=Language.JAVASCRIPT,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.stdout.strip() == "2,4,6"


@pytest.mark.asyncio
async def test_local_timeout_returns_timeout_status(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute(
        "while True:\n    pass\n",
        config=ExecutionConfig(timeout_seconds=0.2),
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert "timed out" in result.stderr.lower()


@pytest.mark.asyncio
async def test_local_security_policy_blocks_dangerous_python(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute("import os\nos.system('echo nope')\n")

    assert result.status is ExecutionStatus.ERROR
    assert "security violation" in result.stderr.lower()
    assert "os.system" in result.stderr


@pytest.mark.asyncio
async def test_local_output_is_truncated(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute(
        "print('x' * 200)",
        config=ExecutionConfig(max_output_bytes=32),
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.stdout.endswith("...[truncated]")
    assert result.metadata["stdout_truncated"] is True


@pytest.mark.asyncio
async def test_local_runtime_errors_are_reported(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute("raise RuntimeError('boom')\n")

    assert result.status is ExecutionStatus.ERROR
    assert "RuntimeError: boom" in result.stderr


@pytest.mark.asyncio
async def test_local_detects_files_created(tmp_path: Path):
    sandbox = LocalCodeSandbox(base_dir=str(tmp_path))

    result = await sandbox.execute(
        "from pathlib import Path\nPath('artifact.txt').write_text('hi')\n"
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.files_created == ["artifact.txt"]


@pytest.mark.asyncio
async def test_local_cleanup_removes_owned_base_directory():
    sandbox = LocalCodeSandbox()

    await sandbox.execute("print('cleanup')")
    base_dir = Path(sandbox.base_dir)

    assert base_dir.exists()

    await sandbox.cleanup()

    assert not base_dir.exists()


class StubSandbox(BaseSandbox):
    def __init__(self, result: ExecutionResult):
        self._result = result
        self.calls: list[tuple[str, Language]] = []

    async def execute(
        self,
        code: str,
        language: Language = Language.PYTHON,
        config: ExecutionConfig | None = None,
    ) -> ExecutionResult:
        self.calls.append((code, language))
        return self._result

    async def execute_file(
        self,
        file_path: str,
        language: Language | None = None,
        config: ExecutionConfig | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_sandbox_toolset_wraps_results():
    sandbox = StubSandbox(
        ExecutionResult(status=ExecutionStatus.SUCCESS, stdout="tool-output\n"),
    )
    toolset = SandboxToolset(sandbox)

    output = await toolset.execute_python("print('tool')")
    tools = toolset.get_tools()

    assert output == "tool-output\n"
    assert sandbox.calls == [("print('tool')", Language.PYTHON)]
    assert [tool.__name__ for tool in tools] == [
        "execute_python",
        "execute_bash",
        "execute_javascript",
    ]


@pytest.mark.asyncio
async def test_remote_sandbox_uses_mockable_http_transport(tmp_path: Path):
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "authorization": request.headers.get("Authorization"),
                "payload": json.loads(request.read().decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "stdout": "remote ok\n",
                "stderr": "",
                "execution_time_ms": 12.5,
                "files_created": ["report.txt"],
                "metadata": {"backend": "mock"},
            },
        )

    sandbox = RemoteCodeSandbox(
        api_url="https://sandbox.example.test",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )

    script_path = tmp_path / "remote.py"
    script_path.write_text("print('remote')", encoding="utf-8")
    result = await sandbox.execute_file(str(script_path))

    assert result.status is ExecutionStatus.SUCCESS
    assert result.stdout == "remote ok\n"
    assert result.files_created == ["report.txt"]
    assert result.metadata["backend"] == "mock"
    assert requests == [
        {
            "method": "POST",
            "url": "https://sandbox.example.test/execute",
            "authorization": "Bearer secret-key",
            "payload": {
                "code": "print('remote')",
                "language": "python",
                "timeout_seconds": 30.0,
                "max_memory_mb": 256,
                "max_output_bytes": 1048576,
                "allow_network": False,
            },
        }
    ]
