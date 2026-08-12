from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ksadk.evaluation import EvaluationRequest, execute_evaluation
from ksadk.evaluation.adapters import TargetAdapterError
from ksadk.evaluation.contracts import (
    EvalCase,
    EvalRunSpec,
    EvalSetVersion,
    EvalTurn,
    EvaluationConfig,
    TargetKind,
    TargetRef,
    TargetRunStatus,
)
from ksadk.evaluation.local_adapter import LocalSourceTargetAdapter, LocalTargetError


def test_local_target_error_uses_common_adapter_error_contract() -> None:
    error = LocalTargetError("LOCAL_TEST_ERROR", "test failure")

    assert isinstance(error, TargetAdapterError)
    assert error.code == "LOCAL_TEST_ERROR"


def _write_langgraph_project(root: Path) -> None:
    (root / "agentengine.yaml").write_text(
        "\n".join(
            (
                "name: local-agent",
                "framework: langgraph",
                "entry_point: agent.py",
                "agent_variable: graph",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "agent.py").write_text("graph = object()\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_local_snapshot_tracks_source_but_excludes_env(tmp_path: Path) -> None:
    _write_langgraph_project(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=first-secret\n", encoding="utf-8")
    adapter = LocalSourceTargetAdapter(timeout_seconds=5)
    target = TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))

    first = await adapter.snapshot(target)
    env_file.write_text("API_KEY=second-secret\n", encoding="utf-8")
    after_env_change = await adapter.snapshot(target)
    (tmp_path / "runtime.log").write_text("ephemeral runtime output\n", encoding="utf-8")
    after_runtime_artifact = await adapter.snapshot(target)
    (tmp_path / "agent.py").write_text("graph = object()\nVERSION = 2\n", encoding="utf-8")
    after_source_change = await adapter.snapshot(target)

    assert first.kind is TargetKind.LOCAL_SOURCE
    assert first.entrypoint == "agent.py"
    assert first.runtime == "langgraph"
    assert first.revision_digest.startswith("sha256:")
    assert first.revision_digest == after_env_change.revision_digest
    assert first.revision_digest == after_runtime_artifact.revision_digest
    assert first.revision_digest != after_source_change.revision_digest
    assert first.metadata["detectedFramework"] == "langgraph"
    assert first.metadata["agentVariable"] == "graph"
    assert "first-secret" not in repr(first.model_dump())


@pytest.mark.asyncio
async def test_local_snapshot_tracks_runtime_data_and_ignores_artifact_case(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)
    adapter = LocalSourceTargetAdapter(timeout_seconds=5)
    target = TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    first = await adapter.snapshot(target)

    data_file = tmp_path / "knowledge.txt"
    data_file.write_text("version one\n", encoding="utf-8")
    after_data_added = await adapter.snapshot(target)
    data_file.write_text("version two\n", encoding="utf-8")
    after_data_changed = await adapter.snapshot(target)
    artifact = tmp_path / "Build"
    artifact.mkdir()
    (artifact / "generated.txt").write_text("generated output\n", encoding="utf-8")
    after_artifact = await adapter.snapshot(target)

    assert first.revision_digest != after_data_added.revision_digest
    assert after_data_added.revision_digest != after_data_changed.revision_digest
    assert after_data_changed.revision_digest == after_artifact.revision_digest


@pytest.mark.asyncio
async def test_local_snapshot_rejects_invalid_kind_and_entrypoint(tmp_path: Path) -> None:
    _write_langgraph_project(tmp_path)
    adapter = LocalSourceTargetAdapter(timeout_seconds=5)

    with pytest.raises(LocalTargetError) as wrong_kind:
        await adapter.snapshot(TargetRef(kind=TargetKind.A2A, locator=str(tmp_path)))
    assert wrong_kind.value.code == "LOCAL_TARGET_KIND_INVALID"

    outside = tmp_path.parent / "outside-agent.py"
    outside.write_text("graph = object()\n", encoding="utf-8")
    with pytest.raises(LocalTargetError) as escaped_entrypoint:
        await adapter.snapshot(
            TargetRef(
                kind=TargetKind.LOCAL_SOURCE,
                locator=str(tmp_path),
                entrypoint="../outside-agent.py",
            )
        )
    assert escaped_entrypoint.value.code == "LOCAL_ENTRYPOINT_INVALID"


@pytest.mark.asyncio
async def test_local_snapshot_rejects_unsupported_framework(tmp_path: Path) -> None:
    (tmp_path / "agentengine.yaml").write_text(
        "name: codex-agent\nframework: codex\n",
        encoding="utf-8",
    )
    adapter = LocalSourceTargetAdapter(timeout_seconds=5)

    with pytest.raises(LocalTargetError) as unsupported:
        await adapter.snapshot(TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path)))

    assert unsupported.value.code == "LOCAL_FRAMEWORK_UNSUPPORTED"


@pytest.mark.asyncio
async def test_local_snapshot_applies_entrypoint_override_to_runtime_detection(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)
    (tmp_path / "alternate.py").write_text("graph = object()\n", encoding="utf-8")
    calls: list[dict] = []

    async def fake_invoke(**kwargs):
        calls.append(kwargs)
        return kwargs["session_id"], {
            "output_text": "alternate answer",
            "usage": {},
            "metadata": {},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(
            kind=TargetKind.LOCAL_SOURCE,
            locator=str(tmp_path),
            entrypoint="alternate.py",
        )
    )
    spec = _run_spec(snapshot)

    await adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert snapshot.entrypoint == "alternate.py"
    assert calls[0]["launch_context"].detection.entry_point == "alternate.py"


@pytest.mark.asyncio
async def test_local_snapshot_replaces_previous_materialized_workspace(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)
    calls: list[dict] = []

    async def fake_invoke(**kwargs):
        calls.append(kwargs)
        return kwargs["session_id"], {
            "output_text": "answer",
            "usage": {},
            "metadata": {},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    first_snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    first_spec = _run_spec(first_snapshot)
    await adapter.run_case(first_spec, first_spec.evalset.cases[0], attempt=1)
    first_workspace = calls[-1]["launch_context"].project_dir

    (tmp_path / "agent.py").write_text("graph = object()\nVERSION = 2\n", encoding="utf-8")
    second_snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    second_spec = _run_spec(second_snapshot, run_id="eval-run-2")
    await adapter.run_case(second_spec, second_spec.evalset.cases[0], attempt=1)

    assert not first_workspace.exists()
    assert calls[-1]["launch_context"].project_dir.exists()


@pytest.mark.asyncio
async def test_local_snapshot_records_git_state_for_nested_project(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "eval@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Eval Test"],
        check=True,
    )
    project = tmp_path / "agents" / "local-agent"
    project.mkdir(parents=True)
    _write_langgraph_project(project)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    adapter = LocalSourceTargetAdapter(timeout_seconds=5)

    clean = await adapter.snapshot(TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(project)))
    (project / "agent.py").write_text("graph = object()\nDIRTY = True\n", encoding="utf-8")
    dirty = await adapter.snapshot(TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(project)))

    assert clean.metadata["gitHead"]
    assert clean.metadata["gitDirty"] is False
    assert dirty.metadata["gitHead"] == clean.metadata["gitHead"]
    assert dirty.metadata["gitDirty"] is True


def _run_spec(snapshot, *, run_id: str = "eval-run-1") -> EvalRunSpec:
    evalset = EvalSetVersion(
        name="local-smoke",
        cases=[EvalCase(id="case-1", input="hello")],
    )
    return EvalRunSpec(id=run_id, evalset=evalset, target=snapshot)


@pytest.mark.asyncio
async def test_local_run_case_invokes_unified_runtime_and_maps_result(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)
    calls: list[dict] = []

    async def fake_invoke(**kwargs):
        calls.append(kwargs)
        return kwargs["session_id"], {
            "output_text": "local answer",
            "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            "metadata": {"runtime": {"duration_ms": 12}},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    spec = _run_spec(snapshot)

    result = await adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert result.status is TargetRunStatus.PASSED
    assert result.output == "local answer"
    assert result.usage.model_dump() == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "reported": True,
    }
    assert result.trace_ref is None
    assert calls[0]["launch_context"].runtime_type == "langgraph"
    assert calls[0]["launch_context"].project_dir != tmp_path
    assert calls[0]["launch_context"].project_dir.is_dir()
    assert (
        calls[0]["launch_context"].project_dir.joinpath("agent.py").read_text(encoding="utf-8")
        == "graph = object()\n"
    )
    assert calls[0]["launch_context"].detection.agent_variable == "graph"
    assert calls[0]["agent_id"] == "local-agent"
    assert calls[0]["user_id"] == "eval-user"
    assert calls[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert calls[0]["model"] is None
    assert callable(calls[0]["session_service_provider"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_result", "expected_status", "expected_code"),
    [
        (
            ("session-1", {"output_text": "", "usage": {}, "metadata": {}}),
            TargetRunStatus.UNAVAILABLE,
            "LOCAL_OUTPUT_UNAVAILABLE",
        ),
    ],
)
async def test_local_run_case_maps_unavailable_output(
    tmp_path: Path,
    runtime_result,
    expected_status: TargetRunStatus,
    expected_code: str,
) -> None:
    _write_langgraph_project(tmp_path)

    async def fake_invoke(**_kwargs):
        return runtime_result

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    spec = _run_spec(snapshot)

    result = await adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert result.status is expected_status
    assert result.error_code == expected_code


@pytest.mark.asyncio
async def test_local_run_case_maps_runtime_error_but_propagates_cancel(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)

    async def failed_invoke(**_kwargs):
        raise RuntimeError("runner failed")

    failed_adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=failed_invoke)
    snapshot = await failed_adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    spec = _run_spec(snapshot)
    failed = await failed_adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert failed.status is TargetRunStatus.ERROR
    assert failed.error_code == "LOCAL_RUNTIME_ERROR"
    assert failed.error_message == "Local Agent runtime failed"

    async def cancelled_invoke(**_kwargs):
        raise asyncio.CancelledError

    cancelled_adapter = LocalSourceTargetAdapter(
        timeout_seconds=5,
        invoke=cancelled_invoke,
    )
    await cancelled_adapter.snapshot(TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path)))
    with pytest.raises(asyncio.CancelledError):
        await cancelled_adapter.run_case(spec, spec.evalset.cases[0], attempt=1)


@pytest.mark.asyncio
async def test_local_run_case_enforces_timeout_without_leaking_runtime_error(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)

    async def slow_invoke(**_kwargs):
        await asyncio.sleep(1)
        raise RuntimeError("secret-token-must-not-be-persisted")

    adapter = LocalSourceTargetAdapter(timeout_seconds=0.01, invoke=slow_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    spec = _run_spec(snapshot)

    result = await adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert result.status is TargetRunStatus.ERROR
    assert result.error_code == "LOCAL_RUNTIME_TIMEOUT"
    assert result.error_message == "Local Agent runtime timed out"
    assert "secret-token" not in repr(result.model_dump())


@pytest.mark.asyncio
async def test_local_run_case_error_trace_points_to_failing_turn(tmp_path: Path) -> None:
    _write_langgraph_project(tmp_path)
    calls: list[dict] = []

    async def fake_invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("second turn failed")
        return kwargs["session_id"], {
            "output_text": "first answer",
            "usage": {},
            "metadata": {},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    case = EvalCase(
        id="case-1",
        turns=[EvalTurn(input="first"), EvalTurn(input="second")],
    )
    spec = EvalRunSpec(
        id="eval-run-1",
        evalset=EvalSetVersion(name="error-trace", cases=[case]),
        target=snapshot,
    )

    result = await adapter.run_case(spec, case, attempt=1)

    assert result.status is TargetRunStatus.ERROR
    assert result.output == ""
    assert result.trace_ref is None


@pytest.mark.asyncio
async def test_local_run_case_executes_materialized_snapshot_after_source_drift(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)
    calls: list[dict] = []

    async def fake_invoke(**kwargs):
        calls.append(kwargs)
        return kwargs["session_id"], {
            "output_text": "snapshot answer",
            "usage": {},
            "metadata": {},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    spec = _run_spec(snapshot)
    (tmp_path / "agent.py").write_text(
        "graph = object()\nCHANGED_AFTER_SNAPSHOT = True\n",
        encoding="utf-8",
    )

    result = await adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert result.status is TargetRunStatus.PASSED
    assert result.output == "snapshot answer"
    assert len(calls) == 1
    runtime_project = calls[0]["launch_context"].project_dir
    assert runtime_project != tmp_path
    assert "CHANGED_AFTER_SNAPSHOT" not in runtime_project.joinpath("agent.py").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_local_run_case_reuses_session_per_attempt_and_isolates_attempts(
    tmp_path: Path,
) -> None:
    _write_langgraph_project(tmp_path)
    calls: list[dict] = []

    async def fake_invoke(**kwargs):
        calls.append(kwargs)
        turn = len(calls)
        return kwargs["session_id"], {
            "output_text": f"answer-{turn}",
            "usage": {
                "input_tokens": turn,
                "output_tokens": 1,
                "total_tokens": turn + 1,
            },
            "metadata": {},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    case = EvalCase(
        id="case-1",
        turns=[EvalTurn(input="first"), EvalTurn(input="second")],
    )
    spec = EvalRunSpec(
        id="eval-run-1",
        evalset=EvalSetVersion(name="multi-turn", cases=[case]),
        target=snapshot,
    )

    first_attempt = await adapter.run_case(spec, case, attempt=1)
    second_attempt = await adapter.run_case(spec, case, attempt=2)

    first_sessions = {calls[0]["session_id"], calls[1]["session_id"]}
    second_sessions = {calls[2]["session_id"], calls[3]["session_id"]}
    assert len(first_sessions) == 1
    assert len(second_sessions) == 1
    assert first_sessions.isdisjoint(second_sessions)
    assert len({call["invocation_id"] for call in calls}) == 4
    assert [call["messages"] for call in calls] == [
        [{"role": "user", "content": "first"}],
        [{"role": "user", "content": "second"}],
        [{"role": "user", "content": "first"}],
        [{"role": "user", "content": "second"}],
    ]
    assert first_attempt.output == "answer-2"
    assert first_attempt.usage.total_tokens == 5
    assert second_attempt.output == "answer-4"
    assert first_attempt.metadata == {
        "runtime": "langgraph",
        "turnCount": 2,
    }


@pytest.mark.asyncio
async def test_local_run_case_cleans_private_session_after_result(tmp_path: Path) -> None:
    _write_langgraph_project(tmp_path)
    services = []
    session_ids: list[str] = []

    async def fake_invoke(**kwargs):
        service = kwargs["session_service_provider"]()
        services.append(service)
        session_ids.append(kwargs["session_id"])
        await service.create_session(
            kwargs["agent_id"],
            kwargs["user_id"],
            kwargs["session_id"],
        )
        return kwargs["session_id"], {
            "output_text": "answer",
            "usage": {},
            "metadata": {},
        }

    adapter = LocalSourceTargetAdapter(timeout_seconds=5, invoke=fake_invoke)
    snapshot = await adapter.snapshot(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path))
    )
    spec = _run_spec(snapshot)

    result = await adapter.run_case(spec, spec.evalset.cases[0], attempt=1)

    assert result.status is TargetRunStatus.PASSED
    assert len(set(map(id, services))) == 1
    assert await services[0].get_session(session_ids[0]) is None


@pytest.mark.asyncio
async def test_local_adk_evaluation_runs_real_adk_runner_and_persists_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "adk-agent"
    project.mkdir()
    (project / "agentengine.yaml").write_text(
        "name: local-adk\nframework: adk\nentry_point: agent.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )
    (project / "agent.py").write_text(
        """from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.genai import types

class DeterministicAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            partial=True,
            content=types.Content(
                role="model",
                parts=[types.Part(text="hello from real adk")],
            ),
        )

root_agent = DeterministicAgent(name="local_adk")
""",
        encoding="utf-8",
    )
    report_root = tmp_path / "reports"
    request = EvaluationRequest(
        evalset=EvalSetVersion(name="adk-smoke", cases=[EvalCase(id="one", input="hello")]),
        target=TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(project)),
        config=EvaluationConfig(),
        reportDir=str(report_root),
    )

    report = await execute_evaluation(request)

    assert report.status == "PASSED", report.model_dump(mode="json")
    assert report.spec.target.runtime == "adk"
    assert report.case_runs[0].target_run.output == "hello from real adk"
    trace_ref = report.case_runs[0].target_run.trace_ref
    assert trace_ref is not None
    assert trace_ref.session_id
    assert trace_ref.invocation_id
    assert (report_root / report.spec.id / "report.json").is_file()
    assert (
        report_root
        / report.spec.id
        / "evidence"
        / trace_ref.session_id
        / f"{trace_ref.invocation_id}.json"
    ).is_file()
