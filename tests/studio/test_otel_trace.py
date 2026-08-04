from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import RunEvent, RunRecord, RunStatus, Usage
from ksadk.studio.otel_trace import OtlpTraceStore
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace

TRACE_ID = "0123456789abcdef0123456789abcdef"


def _event(event_id: int, event_type: str, offset_ms: int, data: dict) -> RunEvent:
    return RunEvent(
        id=event_id,
        run_id="run_otel",
        type=event_type,
        data=data,
        created_at=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)
        + timedelta(milliseconds=offset_ms),
    )


def _fixture(tmp_path: Path) -> tuple[OtlpTraceStore, RunRecord, list[RunEvent]]:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    record = RunRecord(
        id="run_otel",
        build_id="build_otel",
        agent_id="review-helper",
        session_id="ses_otel",
        trace_id=TRACE_ID,
        manifest_sha256="a" * 64,
        runtime_type="codex",
        model="glm-5.2",
        status=RunStatus.COMPLETED,
        input="private prompt must not enter trace",
        output="private completion must not enter trace",
        usage=Usage(
            input_tokens=128,
            output_tokens=32,
            total_tokens=160,
            cached_input_tokens=16,
            reasoning_output_tokens=8,
            reported=True,
            source="codex",
        ),
        started_at=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 4, 4, 0, 1, 340000, tzinfo=timezone.utc),
        duration_ms=1340,
        duration_source="runtime",
    )
    events = [
        _event(1, "run.started", 0, {}),
        _event(
            2,
            "proxy.requested",
            80,
            {
                "responseId": "resp-1",
                "model": "glm-5.2",
                "protocol": "responses-to-chat",
            },
        ),
        _event(
            3,
            "command.started",
            220,
            {
                "callId": "cmd-1",
                "command": "printenv PRIVATE_TOKEN",
                "cwd": "/private/workspace",
            },
        ),
        _event(
            4,
            "command.completed",
            430,
            {
                "callId": "cmd-1",
                "status": "completed",
                "exitCode": 0,
                "durationMs": 210,
                "output": "secret-output-must-not-enter-trace",
            },
        ),
        _event(
            5,
            "proxy.completed",
            980,
            {
                "responseId": "resp-1",
                "model": "glm-5.2",
                "statusCode": 200,
                "durationMs": 900,
                "usage": {
                    "inputTokens": 128,
                    "outputTokens": 32,
                    "totalTokens": 160,
                    "cachedInputTokens": 16,
                    "reasoningOutputTokens": 8,
                },
            },
        ),
        _event(6, "message.completed", 1200, {"text": "private completion"}),
        _event(
            7,
            "run.completed",
            1340,
            {
                "status": "completed",
                "duration_ms": 1340,
                "source": "codex",
            },
        ),
    ]
    return OtlpTraceStore(workspace), record, events


def _attrs(items: list[dict]) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        value = item["value"]
        result[item["key"]] = next(iter(value.values()))
    return result


def test_otlp_store_persists_standard_spans_and_exact_metrics(tmp_path: Path) -> None:
    """Break caught: Trace remains a custom run/events object with guessed metrics."""

    store, record, events = _fixture(tmp_path)
    store.sync(record, events)

    raw = store.get_otlp(TRACE_ID)
    assert list(raw) == ["resourceSpans"]
    resource_spans = raw["resourceSpans"]
    assert len(resource_spans) == 1
    scope_spans = resource_spans[0]["scopeSpans"]
    spans = scope_spans[0]["spans"]
    assert len(spans) == 3
    assert all(span["traceId"] == TRACE_ID for span in spans)
    assert all(len(span["spanId"]) == 16 for span in spans)

    root = next(span for span in spans if not span.get("parentSpanId"))
    model = next(span for span in spans if span["name"] == "chat glm-5.2")
    tool = next(span for span in spans if span["name"] == "execute_tool codex.command")
    assert model["parentSpanId"] == root["spanId"]
    assert tool["parentSpanId"] == root["spanId"]
    assert int(root["endTimeUnixNano"]) - int(root["startTimeUnixNano"]) == 1_340_000_000
    assert root["status"]["code"] == 1
    assert int(model["endTimeUnixNano"]) - int(model["startTimeUnixNano"]) == 900_000_000
    assert int(tool["endTimeUnixNano"]) - int(tool["startTimeUnixNano"]) == 210_000_000

    root_attributes = _attrs(root["attributes"])
    assert root_attributes["gen_ai.usage.input_tokens"] == "128"
    assert root_attributes["gen_ai.usage.output_tokens"] == "32"
    assert root_attributes["agentkit.usage.reported"] is True
    assert root_attributes["agentkit.duration.source"] == "runtime"

    serialized = json.dumps(raw, ensure_ascii=False)
    for forbidden in (
        "private prompt must not enter trace",
        "private completion must not enter trace",
        "printenv PRIVATE_TOKEN",
        "/private/workspace",
        "secret-output-must-not-enter-trace",
    ):
        assert forbidden not in serialized


def test_trace_view_is_lossless_for_span_inspection_and_reports_missing_values(
    tmp_path: Path,
) -> None:
    """Break caught: UI needs custom RunEvent parsing or displays missing metrics as zero."""

    store, record, events = _fixture(tmp_path)
    store.sync(record, events)

    view = store.get_trace_view(TRACE_ID)
    assert view["traceId"] == TRACE_ID
    assert view["runId"] == "run_otel"
    assert view["metrics"] == {
        "durationMs": 1340,
        "durationSource": "runtime",
        "inputTokens": 128,
        "outputTokens": 32,
        "totalTokens": 160,
        "cachedInputTokens": 16,
        "reasoningOutputTokens": 8,
        "usageReported": True,
        "usageSource": "codex",
    }
    assert len(view["spans"]) == 3
    assert all("attributes" in span and "events" in span for span in view["spans"])
    assert view["rawOtlpPath"] == f"/api/v1/traces/{TRACE_ID}/otlp"

    record.usage = Usage()
    record.duration_ms = None
    record.duration_source = None
    record.completed_at = None
    store.sync(record, events[:2])
    missing = store.get_trace_view(TRACE_ID)["metrics"]
    assert missing["usageReported"] is False
    assert missing["inputTokens"] is None
    assert missing["durationMs"] is None


def test_trace_list_is_filterable_without_loading_chat_sessions(tmp_path: Path) -> None:
    """Break caught: Observability can only find a Trace by navigating through Chat."""

    store, record, events = _fixture(tmp_path)
    store.sync(record, events)

    assert store.list_trace_summaries(agent_id="review-helper") == [
        {
            "traceId": TRACE_ID,
            "runId": "run_otel",
            "agentId": "review-helper",
            "sessionId": "ses_otel",
            "runtimeType": "codex",
            "model": "glm-5.2",
            "status": "COMPLETED",
            "startedAt": "2026-08-04T04:00:00Z",
            "durationMs": 1340,
            "durationSource": "runtime",
            "totalTokens": 160,
            "usageReported": True,
            "spanCount": 3,
            "target": {"type": "local", "name": "本地工作区"},
        }
    ]
    assert store.list_trace_summaries(agent_id="another-agent") == []


def test_trace_api_returns_explorer_view_and_raw_otlp_without_chat_redirect(
    tmp_path: Path,
) -> None:
    """Break caught: the only Trace endpoint returns RunEvents for the Chat inspector."""

    service = StudioService(tmp_path)
    record = RunRecord(
        id="run_api_otel",
        build_id="build_api_otel",
        agent_id="review-helper",
        session_id="ses_api_otel",
        trace_id="abcdef0123456789abcdef0123456789",
        runtime_type="codex",
        model="glm-5.2",
        status=RunStatus.COMPLETED,
        input="private",
        usage=Usage(
            input_tokens=8,
            output_tokens=2,
            total_tokens=10,
            reported=True,
            source="codex",
        ),
        started_at=datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 4, 5, 0, 0, 250000, tzinfo=timezone.utc),
        duration_ms=250,
        duration_source="runtime",
    )
    # Runtime lifecycle uses assignment after construction; Pydantic does not
    # validate assignments, so the OTLP adapter must normalize str enums.
    record.status = RunStatus.COMPLETED
    service.event_store.create(record)
    service.event_store.append(record.id, "run.started", {})
    service.event_store.append(record.id, "run.completed", {"status": "completed"})
    service.event_store.save(record)
    app = create_studio_app(
        tmp_path,
        service=StudioService(tmp_path),
        security_enabled=False,
    )

    with TestClient(app) as client:
        listed = client.get("/api/v1/traces", params={"agentId": "review-helper"})
        detail = client.get(f"/api/v1/traces/{record.trace_id}")
        raw = client.get(f"/api/v1/traces/{record.trace_id}/otlp")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["traceId"] == record.trace_id
    assert detail.status_code == 200
    assert detail.json()["runId"] == record.id
    assert detail.json()["status"] == "COMPLETED"
    root = next(span for span in detail.json()["spans"] if not span["parentSpanId"])
    assert root["status"] == "OK"
    assert "events" not in detail.json()
    assert raw.status_code == 200
    assert raw.json()["resourceSpans"][0]["scopeSpans"][0]["spans"]
