from dataclasses import dataclass, field

import pytest

from ksadk.evaluation.contracts import (
    EvalCase,
    EvalRunSpec,
    EvalSetVersion,
    EvalTurn,
    EvaluationConfig,
    TargetKind,
    TargetRef,
    TargetRunStatus,
    UsageSnapshot,
)
from ksadk.evaluation.studio_build_adapter import (
    StudioBuildResolution,
    StudioBuildTargetAdapter,
    StudioBuildTargetError,
)
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.studio.contracts import (
    AgentSpec,
    BuildRecord,
    BuildStatus,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.service import StudioService


@dataclass
class _Usage:
    input_tokens: int = 2
    output_tokens: int = 3
    total_tokens: int = 5
    reported: bool = True


@dataclass
class _Run:
    id: str
    session_id: str
    trace_id: str
    status: str = "COMPLETED"
    output: str = "answer"
    duration_ms: int = 7
    usage: _Usage = field(default_factory=_Usage)
    error: dict | None = None


class _EventStore:
    def events(self, run_id):
        return self.by_run.get(run_id, [])

    by_run = {}


class _RunService:
    def __init__(self):
        self.calls = []
        self.event_store = _EventStore()

    async def events(self, run_id, *, after=0):
        return [event for event in self.event_store.events(run_id) if event.id > after]

    async def run(self, spec, user_input, *, session_id, on_event=None):
        del on_event
        self.calls.append((spec, user_input, session_id))
        run_id = f"run-{len(self.calls)}"
        event = RuntimeEvent.create(
            EventType.TOOL_CALL_BEGIN,
            agent_id="agent-1",
            user_id="eval-user",
            session_id=session_id,
            invocation_id=run_id,
            seq_id=1,
            payload={"call_id": "call-1", "name": "lookup", "args": {}},
        )
        self.event_store.by_run[run_id] = [
            type(
                "StoredEvent",
                (),
                {"id": event.seq_id, "data": {"runtimeEvent": event.to_dict()}},
            )()
        ]
        return _Run(
            id=run_id,
            session_id=session_id,
            trace_id=f"trace-{len(self.calls)}",
            output=f"answer-{len(self.calls)}",
        )


def _resolution() -> StudioBuildResolution:
    return StudioBuildResolution(
        build_id="build-1",
        agent_id="agent-1",
        revision_digest="sha256:bundle",
        runtime="langgraph",
        model="model-1",
        run_spec=object(),
        metadata={"bundleDigest": "sha256:bundle"},
    )


@pytest.mark.asyncio
async def test_studio_build_snapshot_freezes_resolved_build() -> None:
    adapter = StudioBuildTargetAdapter(
        timeout_seconds=5,
        resolve_build=lambda build_id: _resolution(),
        run_service=_RunService(),
    )

    snapshot = await adapter.snapshot(TargetRef(kind=TargetKind.STUDIO_BUILD, locator="build-1"))

    assert snapshot.kind is TargetKind.STUDIO_BUILD
    assert snapshot.entrypoint == "build:build-1"
    assert snapshot.revision_digest == "sha256:bundle"
    assert snapshot.runtime == "langgraph"
    assert snapshot.metadata["buildId"] == "build-1"
    assert snapshot.metadata["model"] == "model-1"


@pytest.mark.asyncio
async def test_studio_build_runs_turns_in_one_attempt_session() -> None:
    service = _RunService()
    adapter = StudioBuildTargetAdapter(
        timeout_seconds=5,
        resolve_build=lambda build_id: _resolution(),
        run_service=service,
    )
    snapshot = await adapter.snapshot(TargetRef(kind=TargetKind.STUDIO_BUILD, locator="build-1"))
    case = EvalCase(
        id="case-1",
        turns=[EvalTurn(input="first"), EvalTurn(input="second")],
    )
    spec = EvalRunSpec(
        id="eval-1",
        evalset=EvalSetVersion(name="build", cases=[case]),
        target=snapshot,
    )

    first = await adapter.run_case(spec, case, attempt=1)
    await adapter.run_case(spec, case, attempt=2)

    assert first.status is TargetRunStatus.PASSED
    assert first.output == "answer-2"
    assert first.usage == UsageSnapshot(
        input_tokens=4,
        output_tokens=6,
        total_tokens=10,
        reported=True,
    )
    assert first.trace_ref.trace_id == "trace-2"
    assert [trace.trace_id for trace in first.trace_refs] == ["trace-1", "trace-2"]
    assert first.tool_calls[0].name == "lookup"
    assert service.calls[0][2] == service.calls[1][2]
    assert service.calls[0][2] != service.calls[2][2]


def test_studio_build_rejects_resolution_for_another_build() -> None:
    adapter = StudioBuildTargetAdapter(
        timeout_seconds=5,
        resolve_build=lambda build_id: _resolution(),
        run_service=_RunService(),
    )

    with pytest.raises(StudioBuildTargetError) as invalid:
        adapter._resolve("other-build")

    assert invalid.value.code == "STUDIO_BUILD_MISMATCH"


def test_studio_target_normalization_treats_locator_as_build_id(tmp_path) -> None:
    service = StudioService(tmp_path)
    service.builds.save(
        BuildRecord(
            id="build-1",
            agent_id="agent-1",
            source_revision=1,
            status=BuildStatus.SUCCEEDED,
            resolved_digest="sha256:resolved",
            runtime_type="langgraph",
            bundle_digest="sha256:bundle",
            artifact_path=".agentkit/artifacts/build-1/agent-bundle.zip",
        )
    )
    target = TargetRef(kind=TargetKind.STUDIO_BUILD, locator="build-1")

    normalized = service._normalize_public_evaluation_target(target)

    assert normalized == target


@pytest.mark.asyncio
async def test_studio_build_evaluation_runs_immutable_framework_bundle(tmp_path) -> None:
    service = StudioService(tmp_path)
    draft = service.create_studio_agent(
        agent_id="eval-graph",
        name="Evaluation Graph",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/eval-graph/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.test/v1/chat/completions",
                credential_ref="env://FIXTURE_API_KEY",
            ),
            instructions=Instructions(system="Return the fixed result."),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.test"])),
        ),
    )
    source = tmp_path / "agents/eval-graph/source/agent.py"
    source.write_text(
        "\n".join(
            (
                "from langgraph.graph import END, START, StateGraph",
                "",
                "def answer(_state):",
                "    return {'output': 'immutable build answer'}",
                "",
                "builder = StateGraph(dict)",
                "builder.add_node('answer', answer)",
                "builder.add_edge(START, 'answer')",
                "builder.add_edge('answer', END)",
                "graph = builder.compile()",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    build = service.builder.build(draft)
    source.write_text("raise RuntimeError('mutable source used')\n", encoding="utf-8")
    (tmp_path / "suite.yaml").write_text(
        """schemaVersion: ksadk.eval/v1
name: build-smoke
cases:
  - id: one
    input: hello
    assertions:
      - type: response.equals
        value: immutable build answer
""",
        encoding="utf-8",
    )

    operation = service.submit_public_evaluation(
        "suite.yaml",
        TargetRef(kind=TargetKind.STUDIO_BUILD, locator=build.id),
        config=EvaluationConfig(),
        idempotency_key="studio-build-eval-1",
    )
    assert operation.metadata["target"] == {
        "kind": "studio_build",
        "label": "Evaluation Graph",
    }
    completed = await service.operations.wait(operation.id)

    assert completed.status == "SUCCEEDED", completed.error
    report = service.list_public_evaluations()[0]
    assert report.spec.target.kind is TargetKind.STUDIO_BUILD
    assert report.spec.target.revision_digest == build.bundle_digest
    assert report.case_runs[0].target_run.output == "immutable build answer"
    assert report.case_runs[0].target_run.trace_ref is not None
