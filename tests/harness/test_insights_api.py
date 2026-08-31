"""P3 补强：Studio/API 实际消费 context_trace / token_report 的 E2E 测试。

链路：ManagedLangGraphEngine(event_sink) → HarnessInsightsRegistry →
/insights HTTP 路由（纯 dict 投影）。
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.insights import HarnessInsightsRegistry, mount_insights
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


class _ScriptedReasoner(HarnessReasoner):
    def __init__(self, turns: list[HarnessReasoningTurn]) -> None:
        self._turns = list(turns)

    async def complete(self, *, model, prompt, messages, tools):
        return self._turns.pop(0)


def _drive(registry: HarnessInsightsRegistry) -> str:
    """跑一次含压缩的 Run，事件经 sink 登记进 registry，返回 run_id。"""
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner(
            [
                HarnessReasoningTurn(final_text="摘要：审批号 AP-1024。"),
                HarnessReasoningTurn(
                    final_text="完成", usage={"input_tokens": 640, "output_tokens": 32}
                ),
            ]
        ),
        context_engine=HarnessContextEngine(),
        event_sink=registry.record,
    )
    history = [
        {"role": "user", "content": f"历史问题 {i} " + "细节" * 400} for i in range(30)
    ]
    request = StartRequest(
        agent_id="ar-1",
        user_id="user-1",
        session_id="sess-1",
        input="总结",
        runtime_type="managed-langgraph",
        metadata={"conversation_history": history, "context_window_tokens": 2048},
    )

    async def drive() -> str:
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://proj-1@2",
            model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
            prompt=PromptSpec(instructions="你是财务分析助手。"),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(request, compiled)
        registry.register_spec(request.session_id, handle.run_id, spec)
        async for _ in engine.stream(handle):
            pass
        return handle.run_id

    return asyncio.run(drive())


def _client(registry: HarnessInsightsRegistry) -> TestClient:
    app = FastAPI()
    mount_insights(app, registry)
    return TestClient(app)


def test_insights_context_trace_e2e():
    registry = HarnessInsightsRegistry()
    run_id = _drive(registry)
    with _client(registry) as client:
        response = client.get(f"/insights/runs/{run_id}/context-trace")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items and items[-1]["manifest_id"].startswith("ctxm_")
    # Actual 闭环经 API 可查（v2 信封整流后投影）。
    assert items[-1]["actual"]["input_tokens"] == 640


def test_insights_token_report_e2e():
    registry = HarnessInsightsRegistry()
    run_id = _drive(registry)
    with _client(registry) as client:
        response = client.get(f"/insights/runs/{run_id}/token-report")
    assert response.status_code == 200
    report = response.json()
    assert report["manifest_count"] >= 1
    assert report["actual_total_input_tokens"] == 640
    assert all(u["manifest_id"] for u in report["usage_events"])


def test_insights_context_inspection_e2e():
    registry = HarnessInsightsRegistry()
    run_id = _drive(registry)
    with _client(registry) as client:
        run_response = client.get(f"/insights/runs/{run_id}/context-inspection")
        session_response = client.get("/insights/sessions/sess-1/context-inspection")
    assert run_response.status_code == 200
    assert session_response.status_code == 200
    report = run_response.json()
    assert report["schema_version"] == 1
    assert report["current"]["actual_input_tokens"] == 640
    assert report["compaction"]["count"] >= 1


def test_insights_compaction_trace_and_session_report():
    registry = HarnessInsightsRegistry()
    run_id = _drive(registry)
    with _client(registry) as client:
        compactions = client.get(f"/insights/runs/{run_id}/compaction-trace").json()
        session_report = client.get("/insights/sessions/sess-1/token-report").json()
    assert compactions["items"][-1]["compaction_id"].startswith("cmp_")
    assert "quality_checks" in compactions["items"][-1]
    assert session_report["actual_total_input_tokens"] == 640


def test_insights_runtime_readiness_for_run_and_session():
    registry = HarnessInsightsRegistry()
    run_id = _drive(registry)
    with _client(registry) as client:
        run_report = client.get(f"/insights/runs/{run_id}/runtime-readiness")
        session_report = client.get("/insights/sessions/sess-1/runtime-readiness")
    assert run_report.status_code == 200
    assert session_report.status_code == 200
    assert run_report.json()["status"] == "warning"  # 未配置正式审批角色
    assert run_report.json()["deployable"] is True
    assert session_report.json()["spec_hash"] == run_report.json()["spec_hash"]


def test_runtime_readiness_requires_registered_public_spec():
    registry = HarnessInsightsRegistry()
    registry.record(
        "sess-x",
        "run-x",
        RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            agent_id="agent-x",
            user_id="user-x",
            session_id="sess-x",
            invocation_id="run-x",
            seq_id=1,
            payload={"status": "completed"},
        ),
    )
    with _client(registry) as client:
        response = client.get("/insights/runs/run-x/runtime-readiness")
    assert response.status_code == 404


def test_insights_unknown_run_404():
    registry = HarnessInsightsRegistry()
    with _client(registry) as client:
        assert client.get("/insights/runs/nope/context-trace").status_code == 404
        assert client.get("/insights/sessions/nope/token-report").status_code == 404


def test_registry_projects_v2_envelope():
    """API 输出边界：登记处返回的事件已整流为 v2 信封（§8）。"""
    registry = HarnessInsightsRegistry()
    _drive(registry)
    events = registry.run_events(_first_run_id(registry))
    assert events and all(e.schema_version == 2 and e.run_id and e.scope_id for e in events)


def _first_run_id(registry: HarnessInsightsRegistry) -> str:
    return registry.run_ids()[0]
