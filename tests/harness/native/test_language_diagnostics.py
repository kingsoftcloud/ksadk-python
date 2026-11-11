"""Interop, freshness, immutable selection and owned cancellation."""

import asyncio
import hashlib
import json
import os
from contextlib import suppress

import pytest

from ksadk.harness.diagnostic_adapters.contracts import DiagnosticDocument
from ksadk.harness.diagnostic_adapters.native_lsp import NativeLanguageDiagnostics
from tests.harness.native.lsp_fixture import profile


def document(path, text):
    return DiagnosticDocument(
        path=path,
        text=text,
        content_digest="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
    )


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
@pytest.mark.parametrize("mode", ["push", "pull"])
async def test_stdio_reports_current_diagnostics_and_syncs_full_incremental_replacement(
    tmp_path, mode
):
    selected, trace = profile(tmp_path, mode=mode)
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "module.ts"
    old = "const BAD = '😀'"
    source.write_text(old)
    client = NativeLanguageDiagnostics(root=root, profile=selected)
    try:
        broken = await client.observe((document("module.ts", old),))
        assert broken[0]["status"] == "observed" and broken[0]["issues"]
        assert broken[0]["related"] == {}  # Outside-root diagnostics are not admitted.
        pid = client.connection.process.pid
        source.write_text("const good = 42")
        fixed = await client.observe((document("module.ts", source.read_text()),))
        assert fixed[0]["issues"] == [] and fixed[0]["document_version"] == 2
        assert client.connection.process.pid == pid
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        assert sum(r.get("method") == "initialize" for r in records) == 1
        change = next(r for r in records if r.get("method") == "textDocument/didChange")
        end = change["params"]["contentChanges"][0]["range"]["end"]
        assert end == {"line": 0, "character": len(old.encode("utf-16-le")) // 2}
    finally:
        await client.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_changed_server_resource_does_not_launch_or_claim_diagnostics(tmp_path):
    selected, trace = profile(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "x.py").write_text("x=1")
    source = tmp_path / "language_server.py"
    source.write_text(source.read_text() + "\n# changed\n")
    client = NativeLanguageDiagnostics(root=root, profile=selected)
    try:
        report = await client.observe((document("x.py", "x=1"),))
        assert report[0]["status"] == "not_run"
        assert not trace.exists()
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant ownership")
@pytest.mark.asyncio
@pytest.mark.local_process_heavy
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_cancel_and_timeout_stop_the_server_and_its_descendant(tmp_path, stop):
    selected, trace = profile(
        tmp_path, timeout=0.3 if stop == "timeout" else 2, startup_delay=0.4
    )
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "x.py").write_text("HANG")
    client = NativeLanguageDiagnostics(root=root, profile=selected)
    task = None
    try:
        # This case exercises a descendant of an initialized peer. Keep cold
        # startup outside the unchanged 0.3s observation timeout below.
        async with asyncio.timeout(3):
            await client._connect()
        task = asyncio.create_task(client.observe((document("x.py", "HANG"),)))
        async with asyncio.timeout(3):
            while not trace.with_suffix(trace.suffix + ".child").exists():
                if task.done():
                    pytest.fail(f"Observation ended before descendant startup: {await task}")
                await asyncio.sleep(0.01)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            report = await task
            assert report[0]["status"] == "not_run"
            assert report[0]["code"] == "language_server_timeout"
        pid = int(trace.with_suffix(trace.suffix + ".pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await asyncio.sleep(1.1)
        assert not trace.with_suffix(trace.suffix + ".late").exists()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        with suppress(FileNotFoundError, ProcessLookupError):
            os.kill(int(trace.with_suffix(trace.suffix + ".child").read_text()), 9)


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
async def test_cold_start_timeout_stops_server_without_waiting_for_a_descendant(tmp_path, monkeypatch):
    selected, trace = profile(tmp_path, timeout=0.3, startup_delay=0.4)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "x.py").write_text("HANG")
    launched = []
    create_process = asyncio.create_subprocess_exec

    async def record_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    client = NativeLanguageDiagnostics(root=root, profile=selected)
    try:
        report = await client.observe((document("x.py", "HANG"),))
        assert report[0]["status"] == "not_run"
        assert report[0]["code"] == "language_server_timeout"
        assert len(launched) == 1 and launched[0].returncode is not None
        assert not trace.with_suffix(trace.suffix + ".child").exists()
        assert client.connection is None
    finally:
        await client.close()


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "nested\\file.py"])
def test_sdk_document_cannot_grant_an_external_path(path):
    with pytest.raises(ValueError):
        document(path, "x=1")
