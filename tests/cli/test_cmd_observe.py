from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from ksadk.cli.cmd_observe import observe
from ksadk.events.canonical import RunProgress, SourceRef
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.local_service import LocalSessionService


async def seed_session(workspace: Path) -> None:
    service = LocalSessionService(project_dir=str(workspace))
    await service.create_session("agent-1", "user-1", "session-1")
    store = RuntimeEventStore(service)
    for event_id, invocation_id in (
        ("evt-1", "run-1"),
        ("evt-2", "run-2"),
        ("evt-3", "run-1"),
    ):
        await store.append_one(
            "session-1",
            RunProgress(
                schema_version=2,
                event_id=event_id,
                seq=0,
                timestamp=1.0,
                run_id=invocation_id,
                scope_id="scope-1",
                source=SourceRef(framework="ksadk"),
                status="running",
            )
        )


def test_observe_export_outputs_machine_readable_result(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(seed_session(tmp_path))
    target = tmp_path / "session.jsonl"

    result = CliRunner().invoke(
        observe,
        [
            "export",
            "--session-id",
            "session-1",
            "--output",
            str(target),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "eventCount": 3,
        "exportedThroughSeqId": 3,
        "firstSeqId": 1,
        "lastSeqId": 3,
        "path": str(target),
    }


def test_observe_export_filters_invocation(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(seed_session(tmp_path))
    target = tmp_path / "run-1.jsonl"

    result = CliRunner().invoke(
        observe,
        [
            "export",
            "--session-id",
            "session-1",
            "--invocation-id",
            "run-1",
            "--output",
            str(target),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["eventCount"] == 2
    assert [json.loads(line)["seq"] for line in target.read_text().splitlines()[1:]] == [
        1,
        3,
    ]


def test_observe_export_pretty_output(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(seed_session(tmp_path))
    target = tmp_path / "session.jsonl"

    result = CliRunner().invoke(
        observe,
        [
            "export",
            "--session-id",
            "session-1",
            "--output",
            str(target),
            "--format",
            "pretty",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "已导出 3 条事件" in result.output
    assert str(target) in result.output


def test_observe_export_reports_stable_local_errors(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(seed_session(tmp_path))
    existing = tmp_path / "existing.jsonl"
    existing.write_text("keep")

    missing_session = CliRunner().invoke(
        observe,
        ["export", "--session-id", "missing", "--output", str(tmp_path / "missing.jsonl")],
    )
    target_exists = CliRunner().invoke(
        observe,
        ["export", "--session-id", "session-1", "--output", str(existing)],
    )
    missing_directory = CliRunner().invoke(
        observe,
        [
            "export",
            "--session-id",
            "session-1",
            "--output",
            str(tmp_path / "not-created" / "session.jsonl"),
        ],
    )

    assert missing_session.exit_code == 2
    assert "SESSION_LOG_SESSION_NOT_FOUND" in missing_session.output
    assert target_exists.exit_code == 2
    assert "SESSION_LOG_TARGET_EXISTS" in target_exists.output
    assert missing_directory.exit_code == 2
    assert "SESSION_LOG_WRITE_FAILED" in missing_directory.output
