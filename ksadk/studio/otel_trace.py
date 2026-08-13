"""OTLP-first trace persistence and Studio read models.

RuntimeEvent remains the live interaction contract.  This module is the only
place that projects those events into telemetry: persisted Trace data is OTLP
JSON, while ``TraceView`` is a lossless, UI-oriented read model derived from
that canonical document.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from ksadk.studio.contracts import RunEvent, RunRecord
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.pagination import keyset_page
from ksadk.studio.workspace import Workspace

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_KIND = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS_CODE = {0: "UNSET", 1: "OK", 2: "ERROR"}
_SENSITIVE_KEYS = {
    "args",
    "command",
    "content",
    "cwd",
    "input",
    "output",
    "prompt",
    "runtimeEvent",
    "secret",
    "text",
    "token",
}

# 内容类字段：本地 Studio 默认放行（localhost 单用户调试），secret/token 永远脱敏
_CONTENT_KEYS = {"args", "command", "content", "input", "output", "prompt", "text", "delta"}


def _trace_content_enabled() -> bool:
    return os.environ.get("KSADK_STUDIO_TRACE_CONTENT", "1") != "0"


def _canonical_trace_id(value: str) -> str:
    normalized = value.strip().lower()
    if _TRACE_ID.fullmatch(normalized):
        return normalized
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _span_id(trace_id: str, identity: str) -> str:
    return hashlib.sha256(f"{trace_id}:{identity}".encode()).hexdigest()[:16]


def _unix_nano(value: datetime) -> int:
    normalized = value.astimezone(timezone.utc)
    return int(normalized.timestamp()) * 1_000_000_000 + normalized.microsecond * 1000


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _truncate(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "value": _otlp_value(value)}
        for key, value in sorted(values.items())
        if value is not None and value != ""
    ]


def _decode_value(value: dict[str, Any]) -> Any:
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [_decode_value(item) for item in value["arrayValue"].get("values", [])]
    return value.get("stringValue", "")


def _decoded_attributes(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {item["key"]: _decode_value(item["value"]) for item in items}


def _safe_event_attributes(data: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    include_content = _trace_content_enabled()
    for key, value in data.items():
        if any(token in key.lower() for token in ("secret", "token")):
            continue
        if key in _SENSITIVE_KEYS:
            if (
                include_content
                and key in _CONTENT_KEYS
                and isinstance(value, (str, int, float, bool))
            ):
                safe[f"agentkit.event.{key}"] = _truncate(_stringify(value), 2048)
            continue
        if isinstance(value, (str, int, float, bool)):
            safe[f"agentkit.event.{key}"] = value
    return safe


def _duration_from(data: dict[str, Any]) -> int | None:
    value = data.get("durationMs", data.get("duration_ms"))
    return max(0, int(value)) if value is not None else None


def _enum_string(value: Any) -> str:
    """Normalize both validated strings and assignment-time ``str`` enums."""

    return str(getattr(value, "value", value))


class OtlpTraceStore:
    """Persist one canonical OTLP JSON document per local Trace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, trace_id: str) -> Path:
        canonical = _canonical_trace_id(trace_id)
        return self.workspace.resolve(Path(".agentkit/traces") / f"{canonical}.otlp.json")

    def sync(self, record: RunRecord, events: list[RunEvent]) -> dict[str, Any]:
        payload = self._build_otlp(record, events)
        self.workspace.atomic_write_text(
            self._path(record.trace_id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return payload

    def get_otlp(self, trace_id: str) -> dict[str, Any]:
        path = self._path(trace_id)
        if not path.is_file():
            raise not_found("trace", trace_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise not_found("trace", trace_id) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("resourceSpans"), list):
            raise not_found("trace", trace_id)
        return payload

    def get_trace_view(self, trace_id: str) -> dict[str, Any]:
        raw = self.get_otlp(trace_id)
        spans = self._spans(raw)
        if not spans:
            raise not_found("trace", trace_id)
        root = next((span for span in spans if not span.get("parentSpanId")), spans[0])
        root_attributes = _decoded_attributes(root.get("attributes", []))
        canonical = root["traceId"]
        duration = root_attributes.get("agentkit.duration.ms")
        usage_reported = bool(root_attributes.get("agentkit.usage.reported", False))
        metrics = {
            "durationMs": int(duration) if duration is not None else None,
            "durationSource": root_attributes.get("agentkit.duration.source"),
            "inputTokens": (
                int(root_attributes["gen_ai.usage.input_tokens"])
                if usage_reported and "gen_ai.usage.input_tokens" in root_attributes
                else None
            ),
            "outputTokens": (
                int(root_attributes["gen_ai.usage.output_tokens"])
                if usage_reported and "gen_ai.usage.output_tokens" in root_attributes
                else None
            ),
            "totalTokens": (
                int(root_attributes["agentkit.usage.total_tokens"])
                if usage_reported and "agentkit.usage.total_tokens" in root_attributes
                else None
            ),
            "cachedInputTokens": (
                int(root_attributes.get("gen_ai.usage.cached_input_tokens", 0))
                if usage_reported
                else None
            ),
            "reasoningOutputTokens": (
                int(root_attributes.get("gen_ai.usage.reasoning_tokens", 0))
                if usage_reported
                else None
            ),
            "usageReported": usage_reported,
            "usageSource": root_attributes.get("agentkit.usage.source"),
        }
        first_resource = (raw.get("resourceSpans") or [{}])[0]
        first_scope = (first_resource.get("scopeSpans") or [{}])[0]
        return {
            "traceId": canonical,
            "rootSpanId": root["spanId"],
            "runId": root_attributes.get("agentkit.run.id", ""),
            "agentId": root_attributes.get("gen_ai.agent.id", ""),
            "sessionId": root_attributes.get("gen_ai.conversation.id", ""),
            "runtimeType": root_attributes.get("agentkit.runtime.type", ""),
            "model": root_attributes.get("gen_ai.request.model", ""),
            "status": root_attributes.get("agentkit.run.status", ""),
            "startedAt": root_attributes.get("agentkit.run.started_at"),
            "target": {"type": "local", "name": "本地工作区"},
            "resource": _decoded_attributes(
                (first_resource.get("resource") or {}).get("attributes", [])
            ),
            "scope": dict(first_scope.get("scope") or {}),
            "metrics": metrics,
            "spans": [self._span_view(span) for span in spans],
            "rawOtlpPath": f"/api/v1/traces/{quote(canonical, safe='')}/otlp",
        }

    def list_trace_summaries(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        summaries = self._trace_summaries(agent_id=agent_id, status=status)
        summaries.sort(
            key=lambda item: (item["startedAt"] or "", item["traceId"]),
            reverse=True,
        )
        return summaries[: max(1, min(limit, 1000))]

    def paginate_trace_summaries(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        query: str = "",
        limit: int = 50,
        cursor: str | None = None,
        sort: str = "startedAt:desc",
    ) -> dict[str, Any]:
        if sort not in {"startedAt:desc", "startedAt:asc"}:
            raise StudioError(
                "PAGINATION_SORT_INVALID",
                "Trace 排序字段无效",
                status_code=422,
                field="sort",
            )
        summaries = self._trace_summaries(
            agent_id=agent_id,
            status=status,
            query=query,
        )
        filters = {
            "agentId": agent_id or "",
            "status": status or "",
            "query": query.strip().lower(),
        }
        return keyset_page(
            summaries,
            key=lambda item: (item["startedAt"] or "", item["traceId"]),
            reverse=sort.endswith(":desc"),
            limit=max(1, min(limit, 1000)),
            cursor=cursor,
            namespace="traces",
            sort=sort,
            filters=filters,
        )

    def _trace_summaries(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        directory = self.workspace.resolve(".agentkit/traces")
        summaries: list[dict[str, Any]] = []
        normalized_query = query.strip().lower()
        for path in directory.glob("*.otlp.json"):
            try:
                view = self.get_trace_view(path.name.removesuffix(".otlp.json"))
            except Exception:  # noqa: BLE001 - a corrupt local trace must not hide valid traces
                continue
            if agent_id and view["agentId"] != agent_id:
                continue
            if status and view["status"] != status:
                continue
            if normalized_query and not any(
                normalized_query in str(view.get(field) or "").lower()
                for field in (
                    "traceId",
                    "runId",
                    "agentId",
                    "sessionId",
                    "runtimeType",
                    "model",
                    "status",
                )
            ):
                continue
            metrics = view["metrics"]
            summaries.append(
                {
                    "traceId": view["traceId"],
                    "runId": view["runId"],
                    "agentId": view["agentId"],
                    "sessionId": view["sessionId"],
                    "runtimeType": view["runtimeType"],
                    "model": view["model"],
                    "status": view["status"],
                    "startedAt": view["startedAt"],
                    "durationMs": metrics["durationMs"],
                    "durationSource": metrics["durationSource"],
                    "inputTokens": metrics["inputTokens"],
                    "outputTokens": metrics["outputTokens"],
                    "totalTokens": metrics["totalTokens"],
                    "usageReported": metrics["usageReported"],
                    "spanCount": len(view["spans"]),
                    "target": view["target"],
                }
            )
        return summaries

    def trace_overview(
        self,
        *,
        range_name: str = "24h",
        agent_id: str | None = None,
        status: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if range_name not in {"24h", "7d"}:
            raise StudioError(
                "TRACE_RANGE_INVALID",
                "Trace 时间范围无效",
                status_code=422,
                field="range",
            )
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if range_name == "24h":
            bucket_delta = timedelta(hours=1)
            bucket_count = 24
            bucket_start = current.replace(minute=0, second=0, microsecond=0)
        else:
            bucket_delta = timedelta(days=1)
            bucket_count = 7
            bucket_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [bucket_start - bucket_delta * index for index in range(bucket_count - 1, -1, -1)]
        buckets = [
            {
                "startedAt": _iso(start),
                "runs": 0,
                "completed": 0,
            }
            for start in starts
        ]
        cutoff = starts[0]
        summaries = self._trace_summaries(agent_id=agent_id, status=status)
        in_range: list[dict[str, Any]] = []
        for item in summaries:
            raw_started_at = item.get("startedAt")
            if not raw_started_at:
                continue
            try:
                started_at = datetime.fromisoformat(
                    str(raw_started_at).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError:
                continue
            if started_at < cutoff or started_at > current:
                continue
            index = int((started_at - cutoff) // bucket_delta)
            if index < 0 or index >= bucket_count:
                continue
            in_range.append(item)
            buckets[index]["runs"] += 1
            if item["status"] == "COMPLETED":
                buckets[index]["completed"] += 1

        completed = [item for item in in_range if item["status"] == "COMPLETED"]
        durations = [
            int(item["durationMs"]) for item in completed if item.get("durationMs") is not None
        ]
        total = len(in_range)
        return {
            "range": range_name,
            "total": total,
            "completed": len(completed),
            "successRate": len(completed) / total if total else None,
            "averageDurationMs": (sum(durations) / len(durations) if durations else None),
            "inputTokens": sum(int(item.get("inputTokens") or 0) for item in in_range),
            "outputTokens": sum(int(item.get("outputTokens") or 0) for item in in_range),
            "totalTokens": sum(int(item.get("totalTokens") or 0) for item in in_range),
            "buckets": buckets,
        }

    def delete(
        self,
        trace_id: str,
        *,
        purge: bool,
        trash_directory: Path | None = None,
    ) -> None:
        path = self._path(trace_id)
        if not path.is_file():
            return
        if purge:
            path.unlink()
            return
        if trash_directory is None:
            raise ValueError("recoverable deletion requires a trash directory")
        destination = self.workspace.resolve(trash_directory / "traces" / path.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))

    def _build_otlp(self, record: RunRecord, events: list[RunEvent]) -> dict[str, Any]:
        trace_id = _canonical_trace_id(record.trace_id)
        root_id = _span_id(trace_id, "root")
        started_at = record.started_at or (
            events[0].created_at if events else datetime.now(timezone.utc)
        )
        root_start = _unix_nano(started_at)
        root_end = (
            root_start + record.duration_ms * 1_000_000
            if record.duration_ms is not None
            else _unix_nano(record.completed_at)
            if record.completed_at is not None
            else _unix_nano(events[-1].created_at)
            if events
            else root_start
        )
        run_status = _enum_string(record.status)
        root_attributes: dict[str, Any] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.id": record.agent_id,
            "gen_ai.agent.name": record.agent_id,
            "gen_ai.conversation.id": record.session_id,
            "gen_ai.request.model": record.model,
            "agentkit.run.id": record.id,
            "agentkit.build.id": record.build_id,
            "agentkit.run.status": run_status,
            "agentkit.run.started_at": _iso(record.started_at),
            "agentkit.runtime.type": record.runtime_type,
            "agentkit.manifest.sha256": record.manifest_sha256,
            "agentkit.usage.reported": record.usage.reported,
            "agentkit.usage.source": record.usage.source,
            "agentkit.duration.ms": record.duration_ms,
            "agentkit.duration.source": record.duration_source,
        }
        if record.usage.reported:
            root_attributes.update(
                {
                    "gen_ai.usage.input_tokens": record.usage.input_tokens,
                    "gen_ai.usage.output_tokens": record.usage.output_tokens,
                    "gen_ai.usage.cached_input_tokens": record.usage.cached_input_tokens,
                    "gen_ai.usage.reasoning_tokens": record.usage.reasoning_output_tokens,
                    "agentkit.usage.total_tokens": record.usage.total_tokens,
                }
            )
        root = {
            "traceId": trace_id,
            "spanId": root_id,
            "name": f"invoke_agent {record.agent_id}",
            "kind": 1,
            "startTimeUnixNano": str(root_start),
            "endTimeUnixNano": str(root_end),
            "attributes": _attributes(root_attributes),
            "events": self._root_events(events),
            "status": {
                "code": (
                    1
                    if run_status == "COMPLETED"
                    else 2
                    if run_status in {"FAILED", "TIMED_OUT", "CANCELLED"}
                    else 0
                )
            },
        }
        spans = [
            root,
            *self._model_spans(trace_id, root_id, events, root_end),
            *self._tool_spans(trace_id, root_id, events, root_end),
        ]
        spans.sort(key=lambda span: (int(span["startTimeUnixNano"]), span["spanId"]))
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": _attributes(
                            {
                                "service.name": "agentkit-studio",
                                "service.namespace": "ksadk",
                                "deployment.environment.name": "local",
                                "agentkit.runtime.target": "local",
                            }
                        )
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "ksadk.studio.trace-adapter", "version": "1"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

    @staticmethod
    def _root_events(events: list[RunEvent]) -> list[dict[str, Any]]:
        excluded = {
            "proxy.requested",
            "proxy.completed",
            "model.requested",
            "model.completed",
            "command.started",
            "command.completed",
            "tool.started",
            "tool.requested",
            "tool.completed",
        }
        return [
            {
                "timeUnixNano": str(_unix_nano(event.created_at)),
                "name": event.type,
                "attributes": _attributes(_safe_event_attributes(event.data)),
            }
            for event in events
            if event.type not in excluded
        ]

    def _model_spans(
        self, trace_id: str, root_id: str, events: list[RunEvent], root_end: int
    ) -> list[dict[str, Any]]:
        starts: list[RunEvent] = []
        by_response: dict[str, RunEvent] = {}
        spans: list[dict[str, Any]] = []
        for event in events:
            if event.type in {"proxy.requested", "model.requested"}:
                starts.append(event)
                response_id = str(event.data.get("responseId") or "")
                if response_id:
                    by_response[response_id] = event
                continue
            if event.type not in {"proxy.completed", "model.completed"}:
                continue
            response_id = str(event.data.get("responseId") or "")
            start = by_response.pop(response_id, None) if response_id else None
            if start is None and starts:
                start = starts.pop(0)
            elif start in starts:
                starts.remove(start)
            if start is None:
                continue
            spans.append(self._model_span(trace_id, root_id, start, event, root_end))
        for start in starts:
            spans.append(self._model_span(trace_id, root_id, start, None, root_end))
        return spans

    @staticmethod
    def _model_span(
        trace_id: str,
        root_id: str,
        start: RunEvent,
        end: RunEvent | None,
        root_end: int,
    ) -> dict[str, Any]:
        model = str(start.data.get("model") or "unknown")
        response_id = str(start.data.get("responseId") or "")
        start_ns = _unix_nano(start.created_at)
        duration = _duration_from(end.data) if end is not None else None
        end_ns = (
            start_ns + duration * 1_000_000
            if duration is not None
            else _unix_nano(end.created_at)
            if end is not None
            else root_end
        )
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "gen_ai.response.id": response_id,
        }
        if end is not None:
            raw_usage = end.data.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            for source_key, target_key in (
                ("inputTokens", "gen_ai.usage.input_tokens"),
                ("outputTokens", "gen_ai.usage.output_tokens"),
                ("cachedInputTokens", "gen_ai.usage.cached_input_tokens"),
                ("reasoningOutputTokens", "gen_ai.usage.reasoning_tokens"),
            ):
                if usage.get(source_key) is not None:
                    attrs[target_key] = int(usage[source_key])
        status_code = int(end.data.get("statusCode") or 200) if end is not None else 0
        return {
            "traceId": trace_id,
            "spanId": _span_id(trace_id, f"model:{start.id}:{response_id}"),
            "parentSpanId": root_id,
            "name": f"chat {model}",
            "kind": 3,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(max(start_ns, end_ns)),
            "attributes": _attributes(attrs),
            "events": [],
            "status": {"code": 1 if 200 <= status_code < 400 else 2 if status_code else 0},
        }

    def _tool_spans(
        self, trace_id: str, root_id: str, events: list[RunEvent], root_end: int
    ) -> list[dict[str, Any]]:
        starts: dict[str, RunEvent] = {}
        spans: list[dict[str, Any]] = []
        for event in events:
            if event.type in {"command.started", "tool.started", "tool.requested"}:
                call_id = str(event.data.get("callId") or event.data.get("call_id") or event.id)
                starts[call_id] = event
                continue
            if event.type not in {"command.completed", "tool.completed"}:
                continue
            call_id = str(event.data.get("callId") or event.data.get("call_id") or "")
            start = starts.pop(call_id, None)
            if start is None:
                continue
            spans.append(self._tool_span(trace_id, root_id, call_id, start, event, root_end))
        for call_id, start in starts.items():
            spans.append(self._tool_span(trace_id, root_id, call_id, start, None, root_end))
        return spans

    @staticmethod
    def _tool_span(
        trace_id: str,
        root_id: str,
        call_id: str,
        start: RunEvent,
        end: RunEvent | None,
        root_end: int,
    ) -> dict[str, Any]:
        name = str(
            start.data.get("tool")
            or ("codex.command" if start.type == "command.started" else "tool")
        )
        start_ns = _unix_nano(start.created_at)
        duration = _duration_from(end.data) if end is not None else None
        end_ns = (
            start_ns + duration * 1_000_000
            if duration is not None
            else _unix_nano(end.created_at)
            if end is not None
            else root_end
        )
        exit_code = end.data.get("exitCode") if end is not None else None
        status = str(end.data.get("status") or "") if end is not None else ""
        failed = (exit_code not in (None, 0)) or status.lower() in {"failed", "error"}
        include_content = _trace_content_enabled()
        tool_input: Any = None
        if include_content:
            if start.type == "command.started":
                tool_input = start.data.get("command") or start.data.get("commandActions")
            elif start.type == "tool.started":
                tool_input = (
                    start.data.get("args") or start.data.get("arguments") or start.data.get("input")
                )
        tool_output: Any = None
        if include_content and end is not None:
            tool_output = (
                end.data.get("result") or end.data.get("output") or end.data.get("content")
            )
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call_id,
            "agentkit.tool.exit_code": exit_code,
            "agentkit.tool.status": status,
        }
        if tool_input is not None:
            attrs["agentkit.tool.input"] = _truncate(_stringify(tool_input))
        if tool_output is not None:
            attrs["agentkit.tool.output"] = _truncate(_stringify(tool_output))
        return {
            "traceId": trace_id,
            "spanId": _span_id(trace_id, f"tool:{call_id}:{start.id}"),
            "parentSpanId": root_id,
            "name": f"execute_tool {name}",
            "kind": 1,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(max(start_ns, end_ns)),
            "attributes": _attributes(attrs),
            "events": [],
            "status": {"code": 2 if failed else 1 if end is not None else 0},
        }

    @staticmethod
    def _spans(raw: dict[str, Any]) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for resource in raw.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                spans.extend(scope.get("spans", []))
        return spans

    @staticmethod
    def _span_view(span: dict[str, Any]) -> dict[str, Any]:
        start_ns = int(span.get("startTimeUnixNano") or 0)
        end_ns = int(span.get("endTimeUnixNano") or start_ns)
        return {
            "spanId": span["spanId"],
            "parentSpanId": span.get("parentSpanId"),
            "name": span.get("name", ""),
            "kind": _SPAN_KIND.get(int(span.get("kind") or 0), "UNSPECIFIED"),
            "status": _STATUS_CODE.get(int((span.get("status") or {}).get("code") or 0), "UNSET"),
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "durationMs": max(0, round((end_ns - start_ns) / 1_000_000, 3)),
            "attributes": _decoded_attributes(span.get("attributes", [])),
            "events": [
                {
                    "name": event.get("name", ""),
                    "timeUnixNano": event.get("timeUnixNano", "0"),
                    "attributes": _decoded_attributes(event.get("attributes", [])),
                }
                for event in span.get("events", [])
            ],
            "links": span.get("links", []),
        }


__all__ = ["OtlpTraceStore"]
