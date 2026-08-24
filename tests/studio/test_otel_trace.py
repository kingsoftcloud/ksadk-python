from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import RunEvent, RunRecord, RunStatus, Usage
from ksadk.studio.errors import StudioError
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


def test_otlp_store_persists_standard_spans_and_exact_metrics(tmp_path: Path, monkeypatch) -> None:
    """Break caught: Trace remains a custom run/events object with guessed metrics."""

    # 脱敏模式：显式关闭内容捕获后，命令/输出等内容不得进入 Trace
    monkeypatch.setenv("KSADK_STUDIO_TRACE_CONTENT", "0")
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


def test_otlp_store_aggregates_content_chunks_into_logical_events(tmp_path: Path) -> None:
    store, record, _ = _fixture(tmp_path)
    events = [
        _event(1, "run.started", 0, {}),
        _event(
            2,
            "thinking.delta",
            10,
            {"text": "分", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(
            3,
            "thinking.delta",
            20,
            {"text": "析", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(
            4,
            "thinking.completed",
            30,
            {"text": "分析", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(
            5,
            "message.delta",
            40,
            {"text": "答", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(
            6,
            "message.completed",
            50,
            {"text": "答案", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(7, "run.completed", 60, {"status": "completed"}),
    ]

    store.sync(record, events)

    raw = store.get_otlp(TRACE_ID)
    root = next(
        span
        for span in raw["resourceSpans"][0]["scopeSpans"][0]["spans"]
        if not span.get("parentSpanId")
    )
    content = [
        event for event in root["events"] if event["name"].startswith(("thinking", "message"))
    ]

    assert [event["name"] for event in content] == ["thinking.completed", "message.completed"]
    assert _attrs(content[0]["attributes"])["agentkit.event.text"] == "分析"
    assert _attrs(content[0]["attributes"])["agentkit.event.delta_count"] == "2"
    assert _attrs(content[1]["attributes"])["agentkit.event.text"] == "答案"
    assert _attrs(content[1]["attributes"])["agentkit.event.delta_count"] == "1"


def test_otlp_store_keeps_partial_content_separate_by_step(tmp_path: Path) -> None:
    store, record, _ = _fixture(tmp_path)
    events = [
        _event(1, "run.started", 0, {}),
        _event(
            2,
            "thinking.delta",
            10,
            {"text": "步", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(
            3,
            "thinking.delta",
            20,
            {"text": "骤一", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-1"}},
        ),
        _event(
            4,
            "thinking.delta",
            30,
            {"text": "步骤二", "runtimeEvent": {"turn_id": "turn-1", "step_id": "step-2"}},
        ),
        _event(5, "run.failed", 40, {"status": "failed"}),
    ]

    store.sync(record, events)

    raw = store.get_otlp(TRACE_ID)
    root = next(
        span
        for span in raw["resourceSpans"][0]["scopeSpans"][0]["spans"]
        if not span.get("parentSpanId")
    )
    thinking = [event for event in root["events"] if event["name"] == "thinking.delta"]

    assert [_attrs(event["attributes"])["agentkit.event.text"] for event in thinking] == [
        "步骤一",
        "步骤二",
    ]
    assert [_attrs(event["attributes"])["agentkit.event.delta_count"] for event in thinking] == [
        "2",
        "1",
    ]


def test_otlp_store_includes_tool_io_when_trace_content_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    """本地 Studio 默认开启内容捕获：工具输入/输出应出现在 tool span 属性中。"""

    monkeypatch.setenv("KSADK_STUDIO_TRACE_CONTENT", "1")
    store, record, events = _fixture(tmp_path)
    store.sync(record, events)

    raw = store.get_otlp(TRACE_ID)
    spans = raw["resourceSpans"][0]["scopeSpans"][0]["spans"]
    tool = next(span for span in spans if span["name"] == "execute_tool codex.command")
    tool_attributes = _attrs(tool["attributes"])
    assert tool_attributes["agentkit.tool.input"] == "printenv PRIVATE_TOKEN"
    assert tool_attributes["agentkit.tool.output"] == "secret-output-must-not-enter-trace"


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


def test_trace_view_recovers_standard_usage_from_model_spans(
    tmp_path: Path,
) -> None:
    """Standard OTLP usage must not depend on the AgentKit reported flag."""

    store, record, events = _fixture(tmp_path)
    record.usage = Usage()
    store.sync(record, events)

    metrics = store.get_trace_view(TRACE_ID)["metrics"]

    assert metrics["usageReported"] is True
    assert metrics["usageSource"] == "gen_ai.usage"
    assert metrics["inputTokens"] == 128
    assert metrics["outputTokens"] == 32
    assert metrics["totalTokens"] == 160
    assert metrics["cachedInputTokens"] == 16
    assert metrics["reasoningOutputTokens"] == 8


def test_trace_view_aggregates_only_token_bearing_leaf_spans(tmp_path: Path) -> None:
    """Parent compatibility counters must not double-count their model child."""

    store, record, events = _fixture(tmp_path)
    record.usage = Usage()
    raw = store.sync(record, events)
    spans = raw["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = next(span for span in spans if not span.get("parentSpanId"))
    model = next(span for span in spans if span["name"] == "chat glm-5.2")
    compatibility_parent = deepcopy(model)
    compatibility_parent.update(
        {
            "spanId": "f" * 16,
            "parentSpanId": root["spanId"],
            "name": "agent compatibility wrapper",
        }
    )
    model["parentSpanId"] = compatibility_parent["spanId"]
    model["attributes"].extend(
        [
            {
                "key": "gen_ai.usage.cache_read.input_tokens",
                "value": {"intValue": "16"},
            },
            {
                "key": "gen_ai.usage.reasoning.output_tokens",
                "value": {"intValue": "8"},
            },
        ]
    )
    spans.append(compatibility_parent)
    store.workspace.atomic_write_text(
        store._path(TRACE_ID),
        json.dumps(raw, ensure_ascii=False),
    )

    metrics = store.get_trace_view(TRACE_ID)["metrics"]

    assert metrics["inputTokens"] == 128
    assert metrics["outputTokens"] == 32
    assert metrics["totalTokens"] == 160
    assert metrics["cachedInputTokens"] == 16
    assert metrics["reasoningOutputTokens"] == 8


def test_trace_view_fills_missing_root_usage_fields_from_leaf_spans(tmp_path: Path) -> None:
    """A partial legacy root is authoritative, while leaves fill only absent fields."""

    store, record, events = _fixture(tmp_path)
    record.usage = Usage(
        input_tokens=129,
        reported=True,
        source="legacy-root",
    )
    raw = store.sync(record, events)
    spans = raw["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = next(span for span in spans if not span.get("parentSpanId"))
    root["attributes"] = [
        item
        for item in root["attributes"]
        if item["key"]
        not in {
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.cached_input_tokens",
            "gen_ai.usage.reasoning_tokens",
            "agentkit.usage.total_tokens",
        }
    ]
    store.workspace.atomic_write_text(
        store._path(TRACE_ID),
        json.dumps(raw, ensure_ascii=False),
    )

    metrics = store.get_trace_view(TRACE_ID)["metrics"]

    assert metrics["inputTokens"] == 129
    assert metrics["outputTokens"] == 32
    assert metrics["totalTokens"] == 161
    assert metrics["cachedInputTokens"] == 16
    assert metrics["reasoningOutputTokens"] == 8
    assert metrics["usageReported"] is True
    assert metrics["usageSource"] == "legacy-root"


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
            "inputTokens": 128,
            "outputTokens": 32,
            "totalTokens": 160,
            "usageReported": True,
            "spanCount": 3,
            "target": {"type": "local", "name": "本地工作区"},
        }
    ]
    assert store.list_trace_summaries(agent_id="another-agent") == []


def test_trace_cursor_is_stable_when_newer_trace_is_inserted(tmp_path: Path) -> None:
    """A new first row must not duplicate or omit records from the next page."""

    store, base, events = _fixture(tmp_path)
    started_at = datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)
    original_ids = [f"{value:032x}" for value in (1, 2, 3)]
    for index, trace_id in enumerate(original_ids, start=1):
        record = base.model_copy(
            deep=True,
            update={
                "id": f"run_page_{index}",
                "trace_id": trace_id,
                "started_at": started_at,
                "completed_at": started_at + timedelta(seconds=index),
            },
        )
        store.sync(record, events)

    first = store.paginate_trace_summaries(limit=2, sort="startedAt:desc")
    assert first["total"] == 3
    assert first["nextCursor"]

    newer = base.model_copy(
        deep=True,
        update={
            "id": "run_page_newer",
            "trace_id": f"{99:032x}",
            "started_at": started_at + timedelta(days=1),
            "completed_at": started_at + timedelta(days=1, seconds=1),
        },
    )
    store.sync(newer, events)

    second = store.paginate_trace_summaries(
        limit=2,
        cursor=first["nextCursor"],
        sort="startedAt:desc",
    )
    combined = [item["traceId"] for item in [*first["items"], *second["items"]]]
    assert combined == sorted(original_ids, reverse=True)
    assert len(combined) == len(set(combined)) == 3


def test_trace_cursor_binds_query_and_server_filters_before_slicing(tmp_path: Path) -> None:
    store, base, events = _fixture(tmp_path)
    for index, (agent_id, model) in enumerate(
        (("alpha-agent", "glm-alpha"), ("beta-agent", "glm-beta")),
        start=10,
    ):
        record = base.model_copy(
            deep=True,
            update={
                "id": f"run_filter_{index}",
                "trace_id": f"{index:032x}",
                "agent_id": agent_id,
                "model": model,
            },
        )
        store.sync(record, events)

    page = store.paginate_trace_summaries(query="beta", limit=1)
    assert page["total"] == 1
    assert page["items"][0]["agentId"] == "beta-agent"

    unfiltered = store.paginate_trace_summaries(limit=1)
    with pytest.raises(StudioError) as raised:
        store.paginate_trace_summaries(
            query="different-query",
            limit=1,
            cursor=unfiltered["nextCursor"],
        )
    assert raised.value.code == "PAGINATION_CURSOR_INVALID"


def test_trace_overview_aggregates_all_filtered_records_not_only_current_page(
    tmp_path: Path,
) -> None:
    store, base, events = _fixture(tmp_path)
    completed = base.model_copy(
        deep=True,
        update={"trace_id": f"{21:032x}", "id": "run_overview_completed"},
    )
    failed = base.model_copy(
        deep=True,
        update={
            "trace_id": f"{22:032x}",
            "id": "run_overview_failed",
            "status": RunStatus.FAILED,
        },
    )
    store.sync(completed, events)
    store.sync(failed, events)

    overview = store.trace_overview(
        range_name="24h",
        now=datetime(2026, 8, 4, 4, 30, tzinfo=timezone.utc),
    )

    assert overview["total"] == 2
    assert overview["completed"] == 1
    assert overview["successRate"] == 0.5
    assert overview["averageDurationMs"] == 1340
    assert overview["totalTokens"] == 320
    assert sum(bucket["runs"] for bucket in overview["buckets"]) == 2


def test_trace_api_returns_explorer_view_and_raw_otlp_without_chat_redirect(
    tmp_path: Path,
) -> None:
    """Break caught: the only Trace endpoint returns RunEvents for the Chat inspector."""

    service = StudioService(tmp_path)
    completed_at = datetime.now(timezone.utc)
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
        started_at=completed_at - timedelta(milliseconds=250),
        completed_at=completed_at,
        duration_ms=250,
        duration_source="runtime",
    )
    # Runtime lifecycle uses assignment after construction; Pydantic does not
    # validate assignments, so the OTLP adapter must normalize str enums.
    record.status = RunStatus.COMPLETED
    service.event_store.create(record)
    service.event_store.save(record)
    service.event_store.trace_store.sync(
        record,
        [
            RunEvent(id=1, run_id=record.id, type="run.started"),
            RunEvent(
                id=2,
                run_id=record.id,
                type="run.completed",
                data={"status": "completed"},
            ),
        ],
    )
    app = create_studio_app(
        tmp_path,
        service=StudioService(tmp_path),
        security_enabled=False,
    )

    with TestClient(app) as client:
        listed = client.get("/api/v1/traces", params={"agentId": "review-helper"})
        overview = client.get(
            "/api/v1/traces/overview",
            params={"range": "24h", "agentId": "review-helper"},
        )
        detail = client.get(f"/api/v1/traces/{record.trace_id}")
        raw = client.get(f"/api/v1/traces/{record.trace_id}/otlp")

    assert listed.status_code == 200
    assert set(listed.json()) == {"items", "nextCursor", "total"}
    assert listed.json()["items"][0]["traceId"] == record.trace_id
    assert overview.status_code == 200
    assert overview.json()["total"] == 1
    assert overview.json()["totalTokens"] == 10
    assert detail.status_code == 200
    assert detail.json()["runId"] == record.id
    assert detail.json()["status"] == "COMPLETED"
    root = next(span for span in detail.json()["spans"] if not span["parentSpanId"])
    assert root["status"] == "OK"
    assert "events" not in detail.json()
    assert raw.status_code == 200
    assert raw.json()["resourceSpans"][0]["scopeSpans"][0]["spans"]
