from __future__ import annotations

import asyncio
import base64
import importlib
import json
import time
from pathlib import Path

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from ksadk.conversations.context import build_history_from_events
from ksadk.conversations.context import canonical_event_type
from ksadk.conversations.model_options import normalize_model_options
from ksadk.conversations.model_context import estimate_text_tokens
from ksadk.conversations.runtime import (
    PreparedConversationTurn,
    _build_runner_request_payload,
    _build_runner_ambient_contexts,
    append_context_checkpoint_event,
    append_run_checkpoint_event,
    append_run_resume_event,
    build_compaction_sse_event,
    build_run_input,
    compact_conversation_history,
    extract_responses_resume_input,
    invoke_conversation_once,
    preview_auto_compaction,
    stream_conversation_turn,
    stream_responses_conversation_turn,
)
from ksadk.runtime_context import (
    PlatformInvocationContext,
    get_current_invocation_context_or_default,
    get_current_account_id,
    get_current_invocation_context,
    get_current_user_id,
    platform_invocation_scope,
)
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.tracing.exporters.inmemory_exporter import InMemoryExporter


class _StubRunner:
    def __init__(self):
        self.detection_result = type("Detection", (), {"name": "demo-agent"})()
        self.calls: list[dict] = []
        self.prepared_models: list[str | None] = []

    def prepare_for_request(self, model):
        self.prepared_models.append(model)

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {"output": "assistant says hi"}


class _TransientFallbackRunner(_StubRunner):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("model unavailable")
        return {"output": "assistant says hi"}

    async def stream(self, input_data: dict):
        self.calls.append(input_data)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("model unavailable")
        yield {"type": "text", "delta": "fallback answer"}
        yield {"type": "final", "output": "fallback answer"}


class _CheckpointMetadataRunner(_StubRunner):
    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {
            "output": "checkpointed",
            "metadata": {
                "agentengine": {
                    "run_id": "run-1",
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": "tenant:agent:sess-1",
                            "checkpoint_id": "ckpt-1",
                        }
                    },
                }
            },
        }


class _CheckpointResumeAdvancedRunner(_StubRunner):
    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {
            "output": "resumed",
            "metadata": {
                "agentengine": {
                    "run_id": "run-1",
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": "tenant:agent:sess-1",
                            "checkpoint_id": "ckpt-after-resume",
                        }
                    },
                }
            },
        }


class _PromptTooLongRunner(_StubRunner):
    def __init__(self):
        super().__init__()
        self.invocation_count = 0

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        self.invocation_count += 1
        if self.invocation_count == 1:
            raise RuntimeError("prompt-too-long")
        return {"output": "compacted answer"}


class _FailingRunner(_StubRunner):
    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        raise RuntimeError("boom")


class _StreamingRunner(_StubRunner):
    def __init__(self):
        super().__init__()
        self.stream_calls: list[dict] = []

    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "text", "delta": "hello"}
        yield {"type": "final", "output": "hello"}


class _CheckpointMetadataStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "text", "delta": "hello"}
        yield {
            "type": "checkpoint",
            "metadata": {
                "agentengine": {
                    "run_id": "run-1",
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": "tenant:agent:sess-1",
                            "checkpoint_id": "ckpt-stream",
                        }
                    },
                }
            },
        }


class _CheckpointMetadataPhaseStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {
            "type": "checkpoint",
            "metadata": {
                "agentengine": {
                    "run_id": "run-1",
                    "phase": "数据清洗完成，等待生成报告",
                    "stage": "清洗聚合指标",
                    "summary": "GMV、转化率和退款率已经聚合完成",
                    "next_action": "恢复后继续生成复盘报告",
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": "tenant:agent:sess-1",
                            "checkpoint_id": "ckpt-business-stage",
                        }
                    },
                }
            },
        }


class _CheckpointMetadataWithoutRunIdStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {
            "type": "checkpoint",
            "metadata": {
                "agentengine": {
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": "tenant:agent:sess-1",
                            "checkpoint_id": "ckpt-stream-after-resume",
                        }
                    },
                }
            },
        }


class _BlockingStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "text", "delta": "hello"}
        await asyncio.Event().wait()


class _ApprovalToolResultStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {
            "type": "tool_call",
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "notes.txt"},
            "run_id": "run-approval",
        }
        yield {
            "type": "tool_result",
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "notes.txt"},
            "tool_output": {
                "ok": False,
                "type": "approval_required",
                "approval_request": {
                    "id": "appr_write",
                    "tool_name": "write_workspace_file",
                    "risk_level": "medium",
                    "side_effects": ["workspace_write"],
                },
            },
            "run_id": "run-approval",
        }
        yield {"type": "final", "output": "should not complete"}


class _SuccessfulToolResultStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {
            "type": "tool_call",
            "tool_name": "list_skills",
            "tool_args": {"include": ["focused"]},
            "run_id": "run-list-skills",
        }
        yield {
            "type": "tool_result",
            "tool_name": "list_skills",
            "tool_args": {"include": ["focused"]},
            "tool_output": {"ok": True, "skills": [{"name": "ppt-translator"}]},
            "run_id": "run-list-skills",
        }
        yield {"type": "final", "output": "done"}


class _ResumeStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "final", "output": "resumed"}


class _CompletedOutputStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "text", "delta": "需要查询。"}
        yield {
            "type": "responses_output",
            "response_id": "resp_native",
            "output": [
                {
                    "id": "fc_123",
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "search",
                    "arguments": '{"q":"openclaw"}',
                    "status": "completed",
                },
                {
                    "id": "rs_123",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "先查资料"}],
                },
            ],
        }
        yield {"type": "final", "output": "需要查询。"}


class _ThinkingStreamingRunner(_StreamingRunner):
    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "thinking", "delta": "先分析问题"}
        yield {"type": "text", "delta": "你好"}
        yield {"type": "final", "output": "你好"}


class _ContextCapturingRunner(_StubRunner):
    def __init__(self):
        super().__init__()
        self.captured_runtime_context = None

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        self.captured_runtime_context = get_current_invocation_context()
        return {"output": "captured"}


class _FakeLongTermMemoryService:
    instances: list["_FakeLongTermMemoryService"] = []

    def __init__(self):
        self.saved: list[dict] = []
        self.__class__.instances.append(self)

    def build_context(self, *, user_id: str, query: str, top_k=None) -> dict | None:
        return None

    def save_event_strings(self, *, user_id: str, event_strings: list[str], metadata=None) -> bool:
        self.saved.append(
            {
                "user_id": user_id,
                "event_strings": event_strings,
                "metadata": dict(metadata or {}),
            }
        )
        return True


class _ExternalModelsAsyncClient:
    def __init__(self, *args, payload=None, error: Exception | None = None, **kwargs):
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, headers: dict | None = None):
        if self._error is not None:
            raise self._error
        request = httpx.Request("GET", url, headers=headers)
        return httpx.Response(200, json=self._payload, request=request)


def _extract_sse_payload(chunks: list[str], event_name: str) -> dict:
    current_event = ""
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
            elif line.startswith("data: ") and current_event == event_name:
                return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"SSE event {event_name!r} not found")


def test_runtime_context_helpers_return_defaults_outside_invocation_scope():
    assert get_current_invocation_context() is None
    context = get_current_invocation_context_or_default()
    assert context.user_id == ""
    assert context.account_id == ""
    assert context.session_id == ""
    assert context.history == []
    assert context.attachments == []
    assert get_current_user_id() == ""
    assert get_current_account_id() == ""
    assert get_current_user_id(default="anonymous") == "anonymous"
    assert get_current_account_id(default="tenantless") == "tenantless"


def test_runtime_context_helpers_read_current_invocation_scope():
    context = PlatformInvocationContext(
        agent_id="demo-agent",
        user_id="user-1",
        account_id="acct-1",
        session_id="sess-1",
        history=[],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="mock",
    )

    with platform_invocation_scope(context):
        assert get_current_user_id() == "user-1"
        assert get_current_account_id() == "acct-1"


@pytest.fixture
def in_memory_trace_exporter():
    provider = TracerProvider()
    exporter = InMemoryExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._set_tracer_provider(provider, log=False)
    yield exporter
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


@pytest.fixture(autouse=True)
def _disable_session_title_ai(monkeypatch):
    class _UnavailableTitleClient:
        @property
        def is_available(self):
            return False

    monkeypatch.setattr(
        "ksadk.conversations.runtime.resolve_session_title_client",
        lambda: _UnavailableTitleClient(),
    )


def test_estimate_text_tokens_is_less_optimistic_for_cjk():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hello world") == 3
    assert estimate_text_tokens("你好世界") == 4
    assert estimate_text_tokens("Agent平台设计") == 6


def test_build_compaction_sse_event_returns_str_with_millisecond_timestamp():
    before_ms = int(time.time() * 1000)
    event = build_compaction_sse_event(
        phase="start",
        trigger="auto",
        compacted_until_seq_id=42,
        total_chars=1200,
        total_estimated_tokens=512,
        group_count=9,
        threshold_percentage=80,
    )
    after_ms = int(time.time() * 1000)

    assert isinstance(event, str)
    assert event.startswith("event: response.compaction.start\n")
    payload_line = event.splitlines()[1]
    assert payload_line.startswith("data: ")
    payload = json.loads(payload_line.removeprefix("data: "))
    assert payload["phase"] == "start"
    assert payload["trigger"] == "auto"
    assert payload["compacted_until_seq_id"] == 42
    assert payload["total_chars"] == 1200
    assert payload["total_estimated_tokens"] == 512
    assert payload["group_count"] == 9
    assert payload["threshold_percentage"] == 80
    assert isinstance(payload["timestamp"], int)
    assert before_ms <= payload["timestamp"] <= after_ms


def test_extract_responses_resume_input_accepts_openai_mcp_approval_response():
    resume_input = extract_responses_resume_input(
        [
            {
                "type": "mcp_approval_response",
                "id": "mcprsp_123",
                "approval_request_id": "appr_123",
                "approve": True,
                "reason": "looks safe",
            }
        ]
    )

    assert resume_input == {
        "type": "mcp_approval_response",
        "id": "mcprsp_123",
        "approval_request_id": "appr_123",
        "approve": True,
        "reason": "looks safe",
    }


def test_extract_responses_resume_input_accepts_ksadk_resume_extension():
    resume_input = extract_responses_resume_input(
        [
            {
                "type": "ksadk_resume",
                "interrupt_id": "intr_123",
                "value": {"answer": "继续", "approved": True},
            }
        ]
    )

    assert resume_input == {
        "type": "ksadk_resume",
        "interrupt_id": "intr_123",
        "value": {"answer": "继续", "approved": True},
    }


def test_extract_responses_resume_input_accepts_openai_function_call_output():
    resume_input = extract_responses_resume_input(
        [
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": {"ok": True},
            }
        ]
    )

    assert resume_input == {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": {"ok": True},
    }


@pytest.mark.asyncio
async def test_build_run_input_projects_history_from_append_only_events(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "hi"}]},
        ),
    )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        messages=[{"role": "user", "content": "follow up"}],
    )

    assert prepared.history == [
        {"role": "user", "content": "hello"},
        {"role": "model", "content": "hi"},
        {"role": "user", "content": "follow up"},
    ]

    events = await service.get_events("sess-1")
    assert [event.event_type for event in events] == [
        "user_message",
        "assistant_message",
        "user_message",
    ]


@pytest.mark.asyncio
async def test_build_run_input_preserves_responses_request_history_when_runtime_session_is_empty(
    monkeypatch,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-responses-history",
        messages=[
            {"role": "user", "content": "写一个python快排的示例"},
            {"role": "assistant", "content": "这是 Python 快速排序示例。"},
            {"role": "user", "content": "用go"},
        ],
    )

    assert prepared.user_input == "用go"
    assert prepared.history == [
        {"role": "user", "content": "写一个python快排的示例"},
        {"role": "model", "content": "这是 Python 快速排序示例。"},
        {"role": "user", "content": "用go"},
    ]


@pytest.mark.asyncio
async def test_build_run_input_deduplicates_responses_request_history_against_session_events(
    monkeypatch,
):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-dup")
    await service.append_event(
        "sess-dup",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "写一个python快排的示例"}]},
        ),
    )
    await service.append_event(
        "sess-dup",
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "这是 Python 快速排序示例。"}]},
        ),
    )
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-dup",
        messages=[
            {"role": "user", "content": "写一个python快排的示例"},
            {"role": "assistant", "content": "这是 Python 快速排序示例。"},
            {"role": "user", "content": "用go"},
        ],
    )

    assert prepared.history == [
        {"role": "user", "content": "写一个python快排的示例"},
        {"role": "model", "content": "这是 Python 快速排序示例。"},
        {"role": "user", "content": "用go"},
    ]


def test_set_conversation_span_attributes_sets_langfuse_and_standard_session_id():
    from ksadk.conversations.runtime import _set_conversation_span_attributes

    class _Span:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, key, value):
            self.attributes[key] = value

    span = _Span()

    _set_conversation_span_attributes(
        span,
        agent_id="agent-demo",
        user_id="user-demo",
        session_id="sess-demo",
        invocation_id="inv-demo",
        runner_name="demo-agent",
        model="glm-5.1",
        response_id="resp-demo",
    )

    assert span.attributes["langfuse.session.id"] == "sess-demo"
    assert span.attributes["session.id"] == "sess-demo"
    assert span.attributes["langfuse.user.id"] == "user-demo"
    assert span.attributes["user.id"] == "user-demo"


@pytest.mark.asyncio
async def test_build_run_input_persists_attachment_results_and_passes_them_to_runner(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    message = {
        "role": "user",
        "content": "[上传文件: resume.pdf]\n张三 8年经验",
        "display_content": "请分析附件\n\n## 附件\n- resume.pdf",
        "parts": [{"text": "请分析附件"}],
        "attachments": [
            {
                "display_name": "resume.pdf",
                "mime_type": "application/pdf",
                "transport": "reference",
                "file_uri": "ksadk-upload://resume",
                "size_bytes": 128,
            }
        ],
        "attachment_results": [
            {
                "display_name": "resume.pdf",
                "mime_type": "application/pdf",
                "transport": "reference",
                "file_uri": "ksadk-upload://resume",
                "size_bytes": 128,
                "kind": "document",
                "status": "ok",
                "warnings": [],
                "extraction_method": "pdf_native",
                "text_excerpt": "张三 8年经验",
                "text": "张三 8年经验",
                "document": {"format": "pdf"},
            }
        ],
    }

    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[message],
    )

    assert prepared.attachments == message["attachments"]
    assert prepared.attachment_results == message["attachment_results"]

    events = await service.get_events(prepared.session_id)
    assert events[0].metadata["attachment_results"] == [
        {
            "display_name": "resume.pdf",
            "mime_type": "application/pdf",
            "transport": "reference",
            "file_uri": "ksadk-upload://resume",
            "size_bytes": 128,
            "kind": "document",
            "status": "ok",
            "warnings": [],
            "extraction_method": "pdf_native",
            "text_excerpt": "张三 8年经验",
            "document": {"format": "pdf"},
        }
    ]

    runner = _StubRunner()
    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=prepared.session_id,
        messages=[message],
        model="gpt-4o",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        session_service_provider=lambda: service,
    )

    assert session_id == prepared.session_id
    assert result["output_text"] == "assistant says hi"
    assert runner.calls[-1]["attachment_results"] == message["attachment_results"]


@pytest.mark.asyncio
async def test_build_run_input_reuses_last_attachment_results_for_follow_up_turn(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    message = {
        "role": "user",
        "content": "[上传文件: resume.txt]\n张三 8年经验",
        "display_content": "请分析附件\n\n## 附件\n- resume.txt",
        "parts": [{"text": "请分析附件"}],
        "attachments": [
            {
                "display_name": "resume.txt",
                "mime_type": "text/plain",
                "transport": "reference",
                "file_uri": "ksadk-upload://resume",
                "size_bytes": 64,
            }
        ],
        "attachment_results": [
            {
                "display_name": "resume.txt",
                "mime_type": "text/plain",
                "transport": "reference",
                "file_uri": "ksadk-upload://resume",
                "size_bytes": 64,
                "kind": "text",
                "status": "ok",
                "warnings": [],
                "extraction_method": "text_decode",
                "text_excerpt": "张三 8年经验",
                "text": "张三 8年经验",
            }
        ],
    }

    first = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[message],
    )
    follow_up = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=first.session_id,
        messages=[{"role": "user", "content": "继续分析"}],
    )

    assert first.current_attachments == message["attachments"]
    assert first.current_attachment_results == message["attachment_results"]
    assert first.has_current_files is True
    assert follow_up.attachments == message["attachments"]
    assert follow_up.attachment_results == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "reference",
            "file_uri": "ksadk-upload://resume",
            "size_bytes": 64,
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "张三 8年经验",
        }
    ]
    assert follow_up.current_attachments == []
    assert follow_up.current_attachment_results == []
    assert follow_up.has_current_files is False

    runner = _StubRunner()
    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=first.session_id,
        messages=[{"role": "user", "content": "继续分析"}],
        model="gpt-4o",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        session_service_provider=lambda: service,
    )

    assert session_id == first.session_id
    assert result["output_text"] == "assistant says hi"
    assert runner.calls[-1]["attachment_results"] == follow_up.attachment_results
    assert runner.calls[-1]["current_attachments"] == []
    assert runner.calls[-1]["current_attachment_results"] == []
    assert runner.calls[-1]["has_current_files"] is False


@pytest.mark.asyncio
async def test_build_run_input_stores_recent_attachment_context_without_inline_data(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    image_b64 = base64.b64encode(b"fake image bytes").decode("ascii")
    first = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "看图"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_b64}",
                    },
                ],
            }
        ],
    )

    session = await service.get_session(first.session_id)
    attachment_context = session.state["__ksadk_attachment_context__"]
    assert attachment_context["attachments"] == [
        {
            "display_name": "uploaded_image",
            "mime_type": "image/png",
            "transport": "inline",
            "size_bytes": len(b"fake image bytes"),
            "is_text": False,
        }
    ]
    assert "data" not in attachment_context["attachments"][0]

    follow_up = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=first.session_id,
        messages=[{"role": "user", "content": "继续"}],
    )

    assert follow_up.attachments == attachment_context["attachments"]
    assert follow_up.current_attachments == []
    assert follow_up.has_current_files is False


@pytest.mark.asyncio
async def test_build_run_input_sanitizes_legacy_recent_attachment_context(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-legacy-inline-context",
    )
    await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=session.id,
        scope="session",
        state_delta={
            "__ksadk_attachment_context__": {
                "attachments": [
                    {
                        "display_name": "legacy.png",
                        "mime_type": "image/png",
                        "transport": "inline",
                        "data": base64.b64encode(b"legacy image").decode("ascii"),
                        "size_bytes": 12,
                    }
                ],
                "attachment_results": [
                    {
                        "display_name": "legacy.png",
                        "mime_type": "image/png",
                        "transport": "inline",
                        "text": "图片摘要",
                        "text_excerpt": "图片摘要",
                    }
                ],
            }
        },
    )

    follow_up = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=session.id,
        messages=[{"role": "user", "content": "继续"}],
    )

    assert follow_up.attachments == [
        {
            "display_name": "legacy.png",
            "mime_type": "image/png",
            "transport": "inline",
            "size_bytes": 12,
        }
    ]
    assert "data" not in json.dumps(follow_up.attachments, ensure_ascii=False)
    assert follow_up.attachment_results == [
        {
            "display_name": "legacy.png",
            "mime_type": "image/png",
            "transport": "inline",
            "text_excerpt": "图片摘要",
        }
    ]
    assert "text" not in follow_up.attachment_results[0]
    assert follow_up.current_attachments == []
    assert follow_up.has_current_files is False


@pytest.mark.asyncio
async def test_build_run_input_detects_current_openai_input_image(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    image_b64 = "iVBORw0KGgo="
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请分析这张图"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_b64}",
                    },
                ],
            }
        ],
        model=None,
    )

    assert prepared.has_current_files is True
    assert prepared.current_attachments == [
        {
            "display_name": "uploaded_image",
            "mime_type": "image/png",
            "transport": "inline",
            "data": image_b64,
            "is_text": False,
            "size_bytes": 8,
        }
    ]
    assert prepared.attachments == prepared.current_attachments
    assert prepared.user_parts[1] == {
        "inlineData": {
            "data": image_b64,
            "mimeType": "image/png",
            "displayName": "uploaded_image",
        }
    }
    assert prepared.input_content == [
        {"type": "input_text", "text": "请分析这张图"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
    ]
    assert prepared.input_messages == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "请分析这张图"},
                {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_build_run_input_detects_openai_input_image_object_url(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    image_b64 = "YWJj"
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
    )

    assert prepared.has_current_files is True
    assert prepared.current_attachments[0]["display_name"] == "uploaded_image"
    assert prepared.current_attachments[0]["mime_type"] == "image/jpeg"
    assert prepared.current_attachments[0]["data"] == image_b64


@pytest.mark.asyncio
async def test_build_run_input_preserves_openai_input_image_remote_url(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    image_url = "https://example.com/diagram.png"
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": image_url,
                    },
                ],
            }
        ],
    )

    assert prepared.has_current_files is True
    assert prepared.current_attachments == [
        {
            "display_name": "uploaded_image",
            "mime_type": "image/*",
            "transport": "reference",
            "file_uri": image_url,
            "is_text": False,
            "size_bytes": None,
            "storage_path": None,
        }
    ]
    assert prepared.attachment_results[0]["warnings"] == [
        "附件内容无法读取，请重新上传或检查文件句柄是否仍可访问。"
    ]


@pytest.mark.asyncio
async def test_build_run_input_detects_openai_input_file_data(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    file_text = "候选人简历内容"
    file_b64 = base64.b64encode(file_text.encode("utf-8")).decode("ascii")
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请总结附件"},
                    {
                        "type": "input_file",
                        "filename": "resume.txt",
                        "file_data": file_b64,
                    },
                ],
            }
        ],
    )

    assert prepared.has_current_files is True
    assert prepared.current_attachments == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "data": file_b64,
            "is_text": True,
            "size_bytes": len(file_text.encode("utf-8")),
        }
    ]
    assert prepared.attachment_results[0]["text"] == file_text
    assert prepared.input_content == [
        {"type": "input_text", "text": "请总结附件"},
        {
            "type": "input_file",
            "filename": "resume.txt",
            "file_data": file_b64,
        },
    ]


@pytest.mark.asyncio
async def test_build_run_input_preserves_openai_input_file_references(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": "report.pdf",
                        "file_url": "https://example.com/report.pdf",
                    },
                    {
                        "type": "input_file",
                        "filename": "uploaded.pdf",
                        "file_id": "file-abc123",
                    },
                ],
            }
        ],
    )

    assert prepared.has_current_files is True
    assert prepared.current_attachments == [
        {
            "display_name": "report.pdf",
            "mime_type": "application/pdf",
            "transport": "reference",
            "file_uri": "https://example.com/report.pdf",
            "is_text": False,
            "size_bytes": None,
            "storage_path": None,
        },
        {
            "display_name": "uploaded.pdf",
            "mime_type": "application/pdf",
            "transport": "reference",
            "file_uri": "file-abc123",
            "is_text": False,
            "size_bytes": None,
            "storage_path": None,
        },
    ]


@pytest.mark.asyncio
async def test_invoke_conversation_once_persists_canonical_turn_events(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        prepare_runner=lambda runner, model: runner.prepare_for_request(model),
    )

    assert result["output_text"] == "assistant says hi"
    assert runner.prepared_models == ["gpt-4o"]
    assert runner.calls[-1]["history"] == [{"role": "user", "content": "hello"}]
    assert runner.calls[-1]["input_content"] == [{"type": "input_text", "text": "hello"}]
    assert runner.calls[-1]["input_messages"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]
    assert runner.calls[-1]["input_parts"] == [{"text": "hello"}]

    events = await service.get_events(session_id)
    session = await service.get_session(session_id)
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert [event.author for event in events] == ["user", "demo-agent", "demo-agent", "demo-agent"]
    assert session is not None
    assert session.title == "hello"
    assert session.title_source == "fallback_first_prompt"
    assert session.first_prompt == "hello"
    assert session.last_prompt == "hello"
    assert session.summary == "assistant says hi"


@pytest.mark.asyncio
async def test_invoke_conversation_once_persists_responses_image_parts_for_replay(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()
    image_url = "data:image/png;base64,aW1hZ2U="

    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请分析这张图"},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        model=None,
        prepare_runner=lambda runner, model: None,
    )

    events = await service.get_events(session_id)
    assert events[0].event_type == "user_message"
    assert events[0].content == {
        "role": "user",
        "parts": [
            {"type": "input_text", "text": "请分析这张图"},
            {"type": "input_image", "image_url": image_url},
        ],
    }


@pytest.mark.asyncio
async def test_invoke_conversation_once_persists_response_id_on_assistant_event(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        response_id="resp_feedback_nonstream",
        prepare_runner=lambda runner, model: runner.prepare_for_request(model),
    )

    events = await service.get_events(session_id)
    assistant_event = next(event for event in events if event.event_type == "assistant_message")
    assert result["response_id"] == "resp_feedback_nonstream"
    assert assistant_event.metadata["response_id"] == "resp_feedback_nonstream"


@pytest.mark.asyncio
async def test_invoke_conversation_once_persists_trace_metadata_for_feedback(
    monkeypatch,
    in_memory_trace_exporter,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        response_id="resp_trace_nonstream",
        prepare_runner=lambda runner, model: runner.prepare_for_request(model),
    )

    events = await service.get_events(session_id)
    assistant_event = next(event for event in events if event.event_type == "assistant_message")
    trace_id = assistant_event.metadata.get("trace_id")
    root_span_id = assistant_event.metadata.get("root_span_id")

    assert trace_id
    assert root_span_id
    assert result["metadata"]["trace_id"] == trace_id
    assert result["metadata"]["root_span_id"] == root_span_id
    exported_trace = in_memory_trace_exporter.get_trace(trace_id)
    assert exported_trace is not None
    assert any(span["span_id"] == root_span_id for span in exported_trace["spans"])


@pytest.mark.asyncio
async def test_invoke_conversation_once_sets_langfuse_trace_io_attributes(
    monkeypatch,
    in_memory_trace_exporter,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        response_id="resp_trace_nonstream",
        prepare_runner=lambda runner, model: runner.prepare_for_request(model),
    )

    exported_trace = in_memory_trace_exporter.get_trace(result["metadata"]["trace_id"])
    assert exported_trace is not None
    root_span = next(
        span for span in exported_trace["spans"] if span["span_id"] == result["metadata"]["root_span_id"]
    )
    assert root_span["name"] == "demo-agent"
    assert root_span["status"]["code"] != "StatusCode.ERROR"
    assert root_span["attributes"]["langfuse.trace.name"] == "demo-agent"
    assert root_span["attributes"]["langfuse.user.id"] == "user-1"
    assert root_span["attributes"]["langfuse.session.id"] == session_id
    assert root_span["attributes"]["langfuse.trace.input"] == "hello"
    assert root_span["attributes"]["langfuse.trace.output"] == "assistant says hi"
    assert root_span["attributes"]["langfuse.observation.input"] == "hello"
    assert root_span["attributes"]["langfuse.observation.output"] == "assistant says hi"
    assert root_span["attributes"]["input.value"] == "hello"
    assert root_span["attributes"]["output.value"] == "assistant says hi"


@pytest.mark.asyncio
async def test_invoke_conversation_once_passes_session_id_to_runner(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert runner.calls[-1]["session_id"] == session_id


@pytest.mark.asyncio
async def test_invoke_conversation_once_passes_model_options_to_runner(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        model_options={"thinking": {"type": "disabled"}},
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert runner.calls[-1]["model_options"] == {
        "thinking": {"type": "disabled"},
        "reasoning": {"effort": "none"},
        "max_reasoning_tokens": 0,
    }


@pytest.mark.asyncio
async def test_invoke_conversation_once_falls_back_on_transient_model_error(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _TransientFallbackRunner()

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="glm-5.2",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert runner.prepared_models == ["glm-5.2", "deepseek-v4-pro"]
    assert runner.calls[-1]["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_stream_conversation_turn_falls_back_before_first_delta(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _TransientFallbackRunner()

    events = [
        event
        async for event in stream_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id=None,
            messages=[{"role": "user", "content": "hello"}],
            model="glm-5.2",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        )
    ]

    assert runner.prepared_models == ["glm-5.2", "deepseek-v4-pro"]
    assert runner.calls[-1]["model"] == "deepseek-v4-pro"
    assert any("fallback answer" in event for event in events)
    assert any("response.completed" in event for event in events)


@pytest.mark.asyncio
async def test_invoke_conversation_once_auto_saves_turn_to_sdk_memory_by_default(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
    monkeypatch.setenv("KSADK_LTM_NAMESPACE", "mem-demo")
    monkeypatch.delenv("KSADK_LTM_AUTO_SAVE", raising=False)
    _FakeLongTermMemoryService.instances.clear()
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        lambda: _FakeLongTermMemoryService(),
    )
    runner = _StubRunner()

    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请记住我偏好简洁回答"},
                    {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
                ],
            }
        ],
        model="qwen3-vl-plus",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    memory_service = _FakeLongTermMemoryService.instances[-1]
    assert len(memory_service.saved) == 1
    saved = memory_service.saved[0]
    assert saved["user_id"] == "user-1"
    assert saved["metadata"]["agent_id"] == "demo-agent"
    assert saved["metadata"]["session_id"] == session_id
    assert saved["metadata"]["model"] == "qwen3-vl-plus"
    assert saved["metadata"]["runner_type"]
    assert saved["metadata"]["invocation_id"]

    persisted_events = [json.loads(item) for item in saved["event_strings"]]
    assert [event["role"] for event in persisted_events] == ["user", "assistant"]
    assert persisted_events[0]["parts"] == [{"text": "请记住我偏好简洁回答"}]
    assert persisted_events[0]["metadata"]["attachments"] == [
        {"kind": "image", "display_name": "uploaded_image", "mime_type": "image/png"}
    ]
    assert persisted_events[1]["parts"] == [{"text": "assistant says hi"}]
    assert "base64" not in json.dumps(persisted_events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_invoke_conversation_once_respects_ltm_auto_save_false(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
    monkeypatch.setenv("KSADK_LTM_NAMESPACE", "mem-demo")
    monkeypatch.setenv("KSADK_LTM_AUTO_SAVE", "false")
    _FakeLongTermMemoryService.instances.clear()
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        lambda: _FakeLongTermMemoryService(),
    )

    await invoke_conversation_once(
        runner=_StubRunner(),
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "不要保存"}],
        model="qwen3-vl-plus",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert _FakeLongTermMemoryService.instances == []


@pytest.mark.asyncio
async def test_stream_conversation_turn_auto_saves_completed_turn(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setenv("KSADK_LTM_BACKEND", "sdk")
    monkeypatch.setenv("KSADK_LTM_NAMESPACE", "mem-demo")
    monkeypatch.delenv("KSADK_LTM_AUTO_SAVE", raising=False)
    _FakeLongTermMemoryService.instances.clear()
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        lambda: _FakeLongTermMemoryService(),
    )
    runner = _StreamingRunner()

    events = [
        event
        async for event in stream_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id=None,
            messages=[{"role": "user", "content": "流式保存测试"}],
            model="qwen3-vl-plus",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    assert any("response.completed" in chunk for chunk in events)
    memory_service = _FakeLongTermMemoryService.instances[-1]
    persisted_events = [json.loads(item) for item in memory_service.saved[0]["event_strings"]]
    assert [event["parts"][0]["text"] for event in persisted_events] == [
        "流式保存测试",
        "hello",
    ]


def test_normalize_model_options_maps_legacy_thinking_disabled_to_reasoning_none():
    normalized = normalize_model_options({"thinking": {"type": "disabled"}})

    assert normalized["thinking"] == {"type": "disabled"}
    assert normalized["reasoning"] == {"effort": "none"}
    assert normalized["max_reasoning_tokens"] == 0


def test_normalize_model_options_maps_enabled_thinking_to_default_reasoning_effort():
    normalized = normalize_model_options({"thinking": {"type": "enabled"}})

    assert normalized["thinking"] == {"type": "enabled"}
    assert normalized["reasoning"] == {"effort": "medium"}
    assert "max_reasoning_tokens" not in normalized


@pytest.mark.asyncio
async def test_invoke_conversation_once_maps_mcp_approval_response_to_runner_resume(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-approval"
    )
    await service.append_event(
        "sess-approval",
        SessionEvent(
            id="evt-approval",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "approval required"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_123",
                    "tool_name": "deploy",
                    "arguments": {"target": "preprod"},
                    "run_id": "run_123",
                }
            },
            invocation_id="inv-approval",
        ),
    )
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-approval",
        messages=[],
        model="gpt-4o",
        resume_input={
            "type": "mcp_approval_response",
            "approval_request_id": "appr_123",
            "approve": True,
            "reason": "looks safe",
        },
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert session_id == "sess-approval"
    assert result["output_text"] == "assistant says hi"
    assert runner.calls[-1]["resume"] is True
    assert runner.calls[-1]["input"] == {
        "type": "mcp_approval_response",
        "approval_request_id": "appr_123",
        "approve": True,
        "reason": "looks safe",
        "tool_name": "deploy",
        "tool_args": {
            "target": "preprod",
            "approval": {
                "approved": True,
                "approval_request_id": "appr_123",
                "reason": "looks safe",
            },
        },
        "approval": {
            "approved": True,
            "approval_request_id": "appr_123",
            "reason": "looks safe",
        },
        "run_id": "run_123",
    }
    events = await service.get_events("sess-approval")
    assert [event.event_type for event in events] == [
        "approval_request",
        "approval_response",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert events[1].metadata["resume_input"]["approval_request_id"] == "appr_123"


@pytest.mark.asyncio
async def test_invoke_conversation_once_executes_approved_builtin_tool_resume(
    monkeypatch,
    tmp_path: Path,
):
    service = InMemorySessionService()
    workspace_ui = tmp_path / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(workspace_ui))
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-tool-approval"
    )
    await append_run_checkpoint_event(
        session_id="sess-tool-approval",
        author="demo-agent",
        run_id="call_write",
        checkpoint_id="ckpt-before-tool",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-tool-approval",
                "checkpoint_id": "ckpt-before-tool",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )
    await service.append_event(
        "sess-tool-approval",
        SessionEvent(
            id="evt-approval",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "approval required"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_write",
                    "tool_name": "write_workspace_file",
                    "arguments": {"path": "notes.txt", "content": "hello"},
                    "run_id": "call_write",
                    "server_label": "ksadk",
                }
            },
            invocation_id="inv-approval",
        ),
    )
    runner = _StubRunner()

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-tool-approval",
        messages=[],
        model="gpt-4o",
        resume_input={
            "type": "mcp_approval_response",
            "approval_request_id": "appr_write",
            "approve": True,
        },
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert (workspace_ui / "workspace" / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert runner.calls[-1]["resume"] is True
    assert runner.calls[-1]["input"]["type"] == "function_call_output"
    assert runner.calls[-1]["input"]["call_id"] == "call_write"
    assert runner.calls[-1]["input"]["output"]["ok"] is True
    events = await service.get_events("sess-tool-approval")
    tool_result = next(event for event in events if event.event_type == "tool_result")
    assert tool_result.metadata["tool_name"] == "write_workspace_file"
    assert tool_result.metadata["tool_output"]["ok"] is True
    receipt = tool_result.metadata["tool_receipt"]
    assert receipt["tool_name"] == "write_workspace_file"
    assert receipt["tool_call_id"] == "call_write"
    assert receipt["run_id"] == "call_write"
    assert receipt["checkpoint_id"] == "ckpt-before-tool"
    assert receipt["framework"] == "langgraph"
    assert receipt["framework_ref"]["langgraph"]["thread_id"] == "tenant:agent:sess-tool-approval"
    assert receipt["status"] == "completed"
    assert receipt["idempotency_key"].startswith("tool_receipt:")


@pytest.mark.asyncio
async def test_invoke_conversation_once_treats_accepted_memory_save_as_completed_receipt(
    monkeypatch,
):
    service = InMemorySessionService()
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(
        "ksadk.conversations.runtime._builtin_tool_callable",
        lambda name: (
            lambda **kwargs: {
                "ok": False,
                "status": "accepted_not_extracted",
                "message": "记忆保存请求已被后端受理，但尚未抽取成可检索记忆。",
                "session_state": 0,
                "session_id": "sess-memory-accepted",
            }
        )
        if name == "save_memory"
        else None,
    )
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-memory-accepted"
    )
    await service.append_event(
        "sess-memory-accepted",
        SessionEvent(
            id="evt-approval",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "approval required"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_save_memory",
                    "tool_name": "save_memory",
                    "arguments": {"content": "favorite_breakfast: 武汉热干面"},
                    "run_id": "call_save_memory",
                    "server_label": "ksadk",
                }
            },
            invocation_id="inv-approval",
        ),
    )
    runner = _StubRunner()

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-memory-accepted",
        messages=[],
        model="gpt-4o",
        resume_input={
            "type": "mcp_approval_response",
            "approval_request_id": "appr_save_memory",
            "approve": True,
        },
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert runner.calls[-1]["input"]["output"]["status"] == "accepted_not_extracted"
    events = await service.get_events("sess-memory-accepted")
    tool_result = next(event for event in events if event.event_type == "tool_result")
    receipt = tool_result.metadata["tool_receipt"]
    assert receipt["tool_name"] == "save_memory"
    assert receipt["status"] == "completed"


@pytest.mark.asyncio
async def test_invoke_conversation_once_replays_existing_tool_receipt_without_side_effect(
    monkeypatch,
    tmp_path: Path,
):
    service = InMemorySessionService()
    workspace_ui = tmp_path / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(workspace_ui))
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-tool-replay"
    )
    await service.append_event(
        "sess-tool-replay",
        SessionEvent(
            id="evt-approval",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "approval required"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_write",
                    "tool_name": "write_workspace_file",
                    "arguments": {"path": "notes.txt", "content": "hello"},
                    "run_id": "call_write",
                    "server_label": "ksadk",
                }
            },
            invocation_id="inv-approval",
        ),
    )
    runner = _StubRunner()
    resume_input = {
        "type": "mcp_approval_response",
        "approval_request_id": "appr_write",
        "approve": True,
    }

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-tool-replay",
        messages=[],
        model="gpt-4o",
        resume_input=resume_input,
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )
    (workspace_ui / "workspace" / "notes.txt").write_text("changed-by-user", encoding="utf-8")

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-tool-replay",
        messages=[],
        model="gpt-4o",
        resume_input=resume_input,
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    assert (workspace_ui / "workspace" / "notes.txt").read_text(encoding="utf-8") == "changed-by-user"
    assert runner.calls[-1]["input"]["type"] == "function_call_output"
    assert runner.calls[-1]["input"]["output"]["ok"] is True
    assert runner.calls[-1]["input"]["output"]["replayed"] is True
    events = await service.get_events("sess-tool-replay")
    tool_results = [event for event in events if event.event_type == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[-1].metadata["tool_receipt"]["replayed"] is True
    assert (
        tool_results[-1].metadata["tool_receipt"]["idempotency_key"]
        == tool_results[0].metadata["tool_receipt"]["idempotency_key"]
    )


@pytest.mark.asyncio
async def test_invoke_conversation_once_binds_platform_invocation_context_and_ambient_contexts(
    monkeypatch,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(
        "ksadk.conversations.runtime._build_runner_ambient_contexts",
        lambda **kwargs: {
            "kb_context": {"formatted_text": "KB facts"},
            "memory_context": {"formatted_text": "Memory facts"},
        },
    )
    runner = _ContextCapturingRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "继续"}],
        model="gpt-4o",
        account_id="acct-1",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        session_service_provider=lambda: service,
    )

    assert result["output_text"] == "captured"
    assert session_id
    assert runner.calls[-1]["kb_context"] == {"formatted_text": "KB facts"}
    assert runner.calls[-1]["memory_context"] == {"formatted_text": "Memory facts"}
    assert runner.calls[-1]["platform_context"]["agent_id"] == "demo-agent"
    assert runner.calls[-1]["platform_context"]["user_id"] == "user-1"
    assert runner.calls[-1]["platform_context"]["account_id"] == "acct-1"
    assert runner.calls[-1]["platform_context"]["session_id"] == session_id
    assert runner.captured_runtime_context is not None
    assert runner.captured_runtime_context.agent_id == "demo-agent"
    assert runner.captured_runtime_context.user_id == "user-1"
    assert runner.captured_runtime_context.account_id == "acct-1"
    assert runner.captured_runtime_context.session_id == session_id
    assert runner.captured_runtime_context.kb_context == {"formatted_text": "KB facts"}
    assert runner.captured_runtime_context.memory_context == {"formatted_text": "Memory facts"}
    assert get_current_invocation_context() is None


def test_build_runner_ambient_contexts_skips_memory_when_disabled(monkeypatch):
    monkeypatch.setenv("KSADK_LTM_AMBIENT_ENABLED", "false")
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(AssertionError("memory ambient should be skipped"))
        ),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="hello",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_skips_kb_when_disabled(monkeypatch):
    monkeypatch.setenv("KSADK_KB_AMBIENT_ENABLED", "0")
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("kb ambient should be skipped"))),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="hello",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_default_on_demand_skips_chitchat(monkeypatch):
    monkeypatch.delenv("KSADK_KB_AMBIENT_POLICY", raising=False)
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(AssertionError("kb ambient should not run for chitchat"))
        ),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("memory ambient should not run for chitchat")
            )
        ),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="你好，请介绍一下你自己",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_non_adk_runner_name_does_not_disable_ambient(monkeypatch):
    class _FakeKnowledgeBaseService:
        def build_context(self, query: str):
            return {"formatted_text": f"kb:{query}"}

    runner = _StubRunner()
    runner.detection_result = type(
        "Detection",
        (),
        {
            "name": "adk-migration-helper",
            "type": type("RunnerType", (), {"value": "langgraph"})(),
        },
    )()

    monkeypatch.delenv("KSADK_KB_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(lambda: _FakeKnowledgeBaseService()),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id="user-1",
        user_input="解释一下 KCE 和 KCF 的区别",
    )

    assert contexts["kb_context"] == {"formatted_text": "kb:解释一下 KCE 和 KCF 的区别"}
    assert contexts["memory_context"] is None


def test_build_runner_ambient_contexts_default_on_demand_loads_memory_for_explicit_recall(
    monkeypatch,
):
    class _FakeMemoryService:
        def build_context(self, *, user_id: str, query: str):
            return {"formatted_text": f"memory:{user_id}:{query}"}

    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(lambda: _FakeMemoryService()),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="你还记得我上次说过的偏好吗？",
    )

    assert contexts["kb_context"] is None
    assert contexts["memory_context"] == {
        "formatted_text": "memory:user-1:你还记得我上次说过的偏好吗？"
    }


def test_build_runner_ambient_contexts_default_on_demand_skips_memory_for_short_term_follow_up(
    monkeypatch,
):
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("memory ambient should not run for short-term follow-up")
            )
        ),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="把前面的回答翻译成英文",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_default_on_demand_skips_memory_for_mixed_short_term_prompt(
    monkeypatch,
):
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("memory ambient should not run for mixed short-term prompt")
            )
        ),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="你还记得刚才的回答吗",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_default_on_demand_loads_memory_for_profile_prompt(
    monkeypatch,
):
    class _FakeMemoryService:
        def build_context(self, *, user_id: str, query: str):
            return {"formatted_text": f"memory:{user_id}:{query}"}

    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(lambda: _FakeMemoryService()),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="按照我的风格来写",
    )

    assert contexts["kb_context"] is None
    assert contexts["memory_context"] == {"formatted_text": "memory:user-1:按照我的风格来写"}


def test_build_runner_ambient_contexts_default_on_demand_loads_kb_for_information_query(
    monkeypatch,
):
    class _FakeKnowledgeBaseService:
        def build_context(self, query: str):
            return {"formatted_text": f"kb:{query}"}

    monkeypatch.delenv("KSADK_KB_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(lambda: _FakeKnowledgeBaseService()),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="查一下云主机现在有哪些机型",
    )

    assert contexts["kb_context"] == {"formatted_text": "kb:查一下云主机现在有哪些机型"}
    assert contexts["memory_context"] is None


def test_build_runner_ambient_contexts_default_on_demand_loads_kb_for_explanatory_query(
    monkeypatch,
):
    class _FakeKnowledgeBaseService:
        def build_context(self, query: str):
            return {"formatted_text": f"kb:{query}"}

    monkeypatch.delenv("KSADK_KB_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(lambda: _FakeKnowledgeBaseService()),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="帮我总结一下 AgentEngine 部署步骤",
    )

    assert contexts["kb_context"] == {"formatted_text": "kb:帮我总结一下 AgentEngine 部署步骤"}
    assert contexts["memory_context"] is None


def test_build_runner_ambient_contexts_drops_kb_error_text_returned_by_service(monkeypatch):
    class _BrokenKnowledgeBaseService:
        def build_context(self, query: str):
            return {"formatted_text": "知识库检索失败: timeout", "query": query}

    monkeypatch.delenv("KSADK_KB_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(lambda: _BrokenKnowledgeBaseService()),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="帮我总结一下 AgentEngine 部署步骤",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_drops_memory_error_text_returned_by_service(monkeypatch):
    class _BrokenMemoryService:
        def build_context(self, *, user_id: str, query: str):
            return {"formatted_text": "长期记忆检索失败: timeout", "query": query}

    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(lambda: _BrokenMemoryService()),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="按照我的风格来写",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_ambient_failures_degrade_quietly(monkeypatch):
    class _BrokenKnowledgeBaseService:
        def build_context(self, query: str):
            raise RuntimeError(f"kb boom: {query}")

    class _BrokenMemoryService:
        def build_context(self, *, user_id: str, query: str):
            raise RuntimeError(f"memory boom: {user_id}:{query}")

    monkeypatch.delenv("KSADK_KB_AMBIENT_POLICY", raising=False)
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.from_env",
        staticmethod(lambda: _BrokenKnowledgeBaseService()),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(lambda: _BrokenMemoryService()),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="你还记得我上次说过的偏好吗？",
    )

    assert contexts == {"kb_context": None, "memory_context": None}


def test_build_runner_ambient_contexts_always_policy_preserves_legacy_behavior(monkeypatch):
    class _FakeMemoryService:
        def build_context(self, *, user_id: str, query: str):
            return {"formatted_text": f"memory:{query}"}

    monkeypatch.setenv("KSADK_LTM_AMBIENT_POLICY", "always")
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService.from_env",
        staticmethod(lambda: _FakeMemoryService()),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(),
        user_id="user-1",
        user_input="你好",
    )

    assert contexts["memory_context"] == {"formatted_text": "memory:你好"}


@pytest.mark.asyncio
async def test_stream_conversation_turn_passes_session_id_to_runner(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StreamingRunner()

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-stream",
    )

    events = []
    async for event in stream_conversation_turn(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=session.id,
        messages=[{"role": "user", "content": "继续"}],
        model="gpt-4o",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        session_service_provider=lambda: service,
    ):
        events.append(event)

    assert events
    assert runner.stream_calls[-1]["session_id"] == session.id


@pytest.mark.asyncio
async def test_stream_conversation_turn_emits_final_text_after_tool_events(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _SuccessfulToolResultStreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id=None,
            messages=[{"role": "user", "content": "记住这个"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    assert any("response.completed" in chunk and '"output_text": "done"' in chunk for chunk in chunks)
    completed_payload = _extract_sse_payload(chunks, "response.completed")
    session_id = completed_payload["session_id"]
    events = await service.get_events(session_id)
    assistant_messages = [event for event in events if event.event_type == "assistant_message"]
    assert assistant_messages[-1].content["parts"][0]["text"] == "done"


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_maps_ksadk_resume_to_runner_resume(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _ResumeStreamingRunner()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-resume-stream"
    )
    await service.append_event(
        "sess-resume-stream",
        SessionEvent(
            id="evt-approval",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "need human input"}]},
            metadata={"interrupt_info": {"id": "intr_123"}},
            invocation_id="inv-approval",
        ),
    )

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-resume-stream",
            messages=[],
            model="gpt-4o",
            resume_input={
                "type": "ksadk_resume",
                "interrupt_id": "intr_123",
                "value": {"answer": "继续", "approved": True},
            },
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    assert runner.stream_calls[-1]["resume"] is True
    assert runner.stream_calls[-1]["input"] == {
        "type": "ksadk_resume",
        "interrupt_id": "intr_123",
        "value": {"answer": "继续", "approved": True},
    }
    assert any(chunk.startswith("event: response.completed\n") for chunk in chunks)
    events = await service.get_events("sess-resume-stream")
    assert "approval_response" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_emits_cancelled_terminal(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _BlockingStreamingRunner()
    chunks: list[str] = []

    async def consume():
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-cancel-stream",
            messages=[{"role": "user", "content": "cancel me"}],
            model="gpt-4o",
            invocation_id="inv-cancel-stream",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        ):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    for _ in range(20):
        if any("response.output_text.delta" in chunk for chunk in chunks):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await task

    events = await service.get_events("sess-cancel-stream")
    statuses = [
        event.content.get("status")
        for event in events
        if event.event_type == "run_status"
    ]
    assert statuses == ["in_progress", "cancelled"]
    assert any(chunk.startswith("event: response.cancelled\n") for chunk in chunks)


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_promotes_gateway_approval_result_to_interrupt(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _ApprovalToolResultStreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-gateway-approval",
            messages=[{"role": "user", "content": "写文件"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    event_names = [
        line.removeprefix("event: ")
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("event: ")
    ]
    assert "response.output_item.done" in event_names
    assert "response.incomplete" in event_names
    assert "response.completed" not in event_names

    incomplete = _extract_sse_payload(chunks, "response.incomplete")
    interrupt = incomplete["incomplete_details"]["ksadk_interrupt"]
    assert interrupt["approval_request_id"] == "appr_write"
    assert interrupt["tool_name"] == "write_workspace_file"
    assert interrupt["arguments"] == {"path": "notes.txt"}

    events = await service.get_events("sess-gateway-approval")
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "tool_call",
        "approval_request",
        "run_status",
    ]


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_adds_tool_receipt_to_tool_result(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _SuccessfulToolResultStreamingRunner()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-tool-receipt"
    )
    await append_run_checkpoint_event(
        session_id="sess-tool-receipt",
        author="demo-agent",
        run_id="run-list-skills",
        checkpoint_id="ckpt-list-skills",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-tool-receipt",
                "checkpoint_id": "ckpt-list-skills",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-tool-receipt",
            messages=[{"role": "user", "content": "列出 skills"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    assert any(chunk.startswith("event: response.completed\n") for chunk in chunks)
    events = await service.get_events("sess-tool-receipt")
    tool_result = next(event for event in events if event.event_type == "tool_result")
    receipt = tool_result.metadata["tool_receipt"]
    assert receipt["tool_name"] == "list_skills"
    assert receipt["tool_call_id"] == "run-list-skills"
    assert receipt["run_id"] == "run-list-skills"
    assert receipt["checkpoint_id"] == "ckpt-list-skills"
    assert receipt["framework"] == "langgraph"
    assert receipt["status"] == "completed"
    assert receipt["idempotency_key"].startswith("tool_receipt:")


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_replays_completed_output_items(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _CompletedOutputStreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-native-output",
            messages=[{"role": "user", "content": "查一下"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    event_names = [
        line.removeprefix("event: ")
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("event: ")
    ]
    assert "response.function_call_arguments.done" in event_names
    assert "response.reasoning.delta" in event_names

    completed_payload = None
    current_event = ""
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
            elif line.startswith("data: ") and current_event == "response.completed":
                completed_payload = json.loads(line.removeprefix("data: "))
    assert completed_payload is not None
    assert completed_payload["id"] == "resp_native"
    assert any(item.get("type") == "function_call" for item in completed_payload["output"])


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_persists_outer_response_id_on_assistant_event(
    monkeypatch,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-stream-feedback",
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    created_payload = _extract_sse_payload(chunks, "response.created")
    completed_payload = _extract_sse_payload(chunks, "response.completed")
    events = await service.get_events("sess-stream-feedback")
    assistant_event = next(event for event in events if event.event_type == "assistant_message")
    assert completed_payload["id"] == created_payload["id"]
    assert assistant_event.metadata["response_id"] == created_payload["id"]


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_persists_trace_metadata_for_feedback(
    monkeypatch,
    in_memory_trace_exporter,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-stream-trace-feedback",
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    completed_payload = _extract_sse_payload(chunks, "response.completed")
    events = await service.get_events("sess-stream-trace-feedback")
    assistant_event = next(event for event in events if event.event_type == "assistant_message")
    trace_id = assistant_event.metadata.get("trace_id")
    root_span_id = assistant_event.metadata.get("root_span_id")

    assert trace_id
    assert root_span_id
    assert completed_payload["metadata"]["trace_id"] == trace_id
    assert completed_payload["metadata"]["root_span_id"] == root_span_id
    exported_trace = in_memory_trace_exporter.get_trace(trace_id)
    assert exported_trace is not None
    assert any(span["span_id"] == root_span_id for span in exported_trace["spans"])


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_emits_trace_metadata_from_created_event(
    monkeypatch,
    in_memory_trace_exporter,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-stream-created-trace",
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    created_payload = _extract_sse_payload(chunks, "response.created")
    completed_payload = _extract_sse_payload(chunks, "response.completed")
    assert created_payload["metadata"]["trace_id"]
    assert created_payload["metadata"]["root_span_id"]
    assert created_payload["metadata"]["trace_id"] == completed_payload["metadata"]["trace_id"]
    assert (
        created_payload["metadata"]["root_span_id"]
        == completed_payload["metadata"]["root_span_id"]
    )

    exported_trace = in_memory_trace_exporter.get_trace(created_payload["metadata"]["trace_id"])
    root_span = next(
        span
        for span in exported_trace["spans"]
        if span["span_id"] == created_payload["metadata"]["root_span_id"]
    )
    assert root_span["name"] == "demo-agent"
    assert root_span["attributes"]["langfuse.trace.input"] == "hello"
    assert root_span["attributes"]["langfuse.trace.output"] == "hello"


@pytest.mark.asyncio
async def test_stream_responses_conversation_turn_persists_reasoning_events(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _ThinkingStreamingRunner()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-reasoning",
            messages=[{"role": "user", "content": "你好"}],
            model="gpt-4o",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            session_service_provider=lambda: service,
        )
    ]

    assert any(chunk.startswith("event: response.reasoning.delta\n") for chunk in chunks)
    events = await service.get_events("sess-reasoning")
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "reasoning",
        "assistant_message",
        "run_status",
    ]
    assert events[2].content["parts"][0]["text"] == "先分析问题"


@pytest.mark.asyncio
async def test_stream_responses_turn_maps_function_call_output_without_pending_approval(
    monkeypatch,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _ResumeStreamingRunner()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-tool-output"
    )

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-tool-output",
            messages=[],
            model="gpt-4o",
            resume_input={
                "type": "function_call_output",
                "call_id": "call_123",
                "output": {"ok": True},
            },
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
            request_metadata={"previous_response_id": "resp_123"},
            session_service_provider=lambda: service,
        )
    ]

    assert runner.stream_calls[-1]["resume"] is True
    assert runner.stream_calls[-1]["input"] == {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": {"ok": True},
    }
    assert runner.stream_calls[-1]["previous_response_id"] == "resp_123"
    assert any(chunk.startswith("event: response.completed\n") for chunk in chunks)
    events = await service.get_events("sess-tool-output")
    assert "tool_result" in [event.event_type for event in events]
    assert "approval_response" not in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_invoke_conversation_once_refines_session_title_after_first_turn(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    class _FakeTitleClient:
        @property
        def is_available(self):
            return True

        async def generate_title(self, *, model, messages, timeout_ms):
            assert model == "glm-5.1"
            assert messages[0]["role"] == "system"
            assert "你好，请介绍一下你自己" in messages[-1]["content"]
            return "自我介绍", {"total_tokens": 12}

    monkeypatch.setattr(
        "ksadk.conversations.runtime.resolve_session_title_client",
        lambda: _FakeTitleClient(),
    )

    runner = _StubRunner()
    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "你好，请介绍一下你自己"}],
        model="glm-5.1",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    session = await service.get_session(session_id)
    assert session is not None
    assert session.first_prompt == "你好，请介绍一下你自己"
    for _ in range(20):
        if session.title == "自我介绍":
            break
        await asyncio.sleep(0.01)
        session = await service.get_session(session_id)
        assert session is not None
    assert session.title == "自我介绍"
    assert session.title_source == "ai"


@pytest.mark.asyncio
async def test_invoke_conversation_once_does_not_wait_for_ai_session_title(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    title_started = asyncio.Event()
    release_title = asyncio.Event()

    class _BlockingTitleClient:
        @property
        def is_available(self):
            return True

        async def generate_title(self, *, model, messages, timeout_ms):
            title_started.set()
            await release_title.wait()
            return "自我介绍", {"total_tokens": 12}

    monkeypatch.setattr(
        "ksadk.conversations.runtime.resolve_session_title_client",
        lambda: _BlockingTitleClient(),
    )

    runner = _StubRunner()
    invoke_task = asyncio.create_task(
        invoke_conversation_once(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id=None,
            messages=[{"role": "user", "content": "你好，请介绍一下你自己"}],
            model="glm-5.1",
            prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        )
    )
    await asyncio.wait_for(title_started.wait(), timeout=1)

    session_id, _ = await asyncio.wait_for(invoke_task, timeout=0.2)
    session = await service.get_session(session_id)
    assert session is not None
    assert session.title == "Agent能力介绍"
    assert session.title_source == "heuristic"

    release_title.set()
    for _ in range(20):
        session = await service.get_session(session_id)
        assert session is not None
        if session.title == "自我介绍":
            break
        await asyncio.sleep(0.01)
    assert session.title == "自我介绍"
    assert session.title_source == "ai"


@pytest.mark.asyncio
async def test_invoke_conversation_once_uses_heuristic_title_for_agent_intro(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    class _IntroRunner(_StubRunner):
        async def invoke(self, input_data: dict) -> dict:
            self.calls.append(input_data)
            return {
                "output": (
                    "你好！我是企业高端招聘全流程助手，可以协助你完成职位分析、"
                    "候选人筛选和面试建议生成。"
                )
            }

    runner = _IntroRunner()
    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "你好，请介绍一下你自己"}],
        model="glm-5.1",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    session = await service.get_session(session_id)
    assert session is not None
    assert session.title == "招聘助手能力"
    assert session.title_source == "heuristic"


@pytest.mark.asyncio
async def test_invoke_conversation_once_strips_inline_think_markup_from_output(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    class _ThinkingTagRunner(_StubRunner):
        async def invoke(self, input_data: dict) -> dict:
            self.calls.append(input_data)
            return {"output": "<think>先判断问题。</think>我是招聘助手。"}

    runner = _ThinkingTagRunner()
    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "你好，请介绍一下你自己"}],
        model="glm-5.1",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    events = await service.get_events(session_id)
    assistant_event = next(event for event in events if event.event_type == "assistant_message")
    session = await service.get_session(session_id)

    assert result["output_text"] == "我是招聘助手。"
    assert assistant_event.content["parts"][0]["text"] == "我是招聘助手。"
    assert session is not None
    assert session.summary == "我是招聘助手。"
    assert session.title == "招聘助手能力"


@pytest.mark.asyncio
async def test_invoke_conversation_once_uses_heuristic_title_for_architecture_attachment(
    monkeypatch,
):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    class _ArchitectureRunner(_StubRunner):
        async def invoke(self, input_data: dict) -> dict:
            self.calls.append(input_data)
            return {
                "output": (
                    "这张图展示了典型的微服务分层架构，"
                    "包含网关、业务服务、数据库和异步消息链路。"
                )
            }

    runner = _ArchitectureRunner()
    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "看看这个上传文件，直接开始分析吧，这里还有他画的架构图",
                    },
                    {
                        "type": "input_file",
                        "fileData": {
                            "fileUri": "ksadk-upload://arch.png",
                            "displayName": "架构.png",
                            "mimeType": "image/png",
                        },
                    },
                ],
            }
        ],
        model="glm-5.1",
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
    )

    session = await service.get_session(session_id)
    assert session is not None
    assert session.title == "架构图分析"
    assert session.title_source == "heuristic"


@pytest.mark.asyncio
async def test_append_context_checkpoint_event_records_compaction_boundary(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    event = await append_context_checkpoint_event(
        session_id="sess-1",
        author="demo-agent",
        compacted_until_seq_id=8,
        metadata={"reason": "auto-compact"},
    )

    assert event.event_type == "context_checkpoint"
    assert event.metadata["compacted_until_seq_id"] == 8
    assert event.metadata["reason"] == "auto-compact"


def test_session_event_infers_canonical_message_types():
    user_event = SessionEvent.from_dict(
        {
            "author": "user",
            "content": {"role": "user", "parts": [{"text": "hello"}]},
        }
    )
    assistant_event = SessionEvent.from_dict(
        {
            "author": "demo-agent",
            "content": {"role": "model", "parts": [{"text": "hi"}]},
        }
    )

    assert user_event.event_type == "user_message"
    assert assistant_event.event_type == "assistant_message"


def test_runtime_checkpoint_events_are_canonical_but_not_projected_to_history():
    events = [
        SessionEvent(
            id="evt-1",
            author="demo-agent",
            event_type="run_checkpoint",
            content={"text": "checkpoint saved"},
            metadata={"run_id": "run-1", "checkpoint_id": "ckpt-1"},
            seq_id=1,
        ),
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="run_resume",
            content={"text": "resume requested"},
            metadata={"run_id": "run-1", "resume_attempt_id": "resume-1"},
            seq_id=2,
        ),
        SessionEvent(
            id="evt-3",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "继续"}]},
            seq_id=3,
        ),
    ]

    assert canonical_event_type("run_checkpoint") == "run_checkpoint"
    assert canonical_event_type("run_resume") == "run_resume"
    assert build_history_from_events(events) == [{"role": "user", "content": "继续"}]


@pytest.mark.asyncio
async def test_append_run_checkpoint_and_resume_events(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    checkpoint = await append_run_checkpoint_event(
        session_id="sess-1",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-1",
                "checkpoint_id": "ckpt-1",
            }
        },
        phase="tool_result",
        invocation_id="inv-1",
    )
    resume = await append_run_resume_event(
        session_id="sess-1",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        resume_attempt_id="resume-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-1",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-2",
    )

    assert checkpoint.event_type == "run_checkpoint"
    assert checkpoint.metadata["run_id"] == "run-1"
    assert checkpoint.metadata["checkpoint_id"] == "ckpt-1"
    assert checkpoint.metadata["framework_ref"]["langgraph"]["thread_id"] == "tenant:agent:sess-1"
    assert resume.event_type == "run_resume"
    assert resume.metadata["resume_attempt_id"] == "resume-1"


def test_extract_responses_resume_input_accepts_checkpoint_resume_action():
    resume_input = extract_responses_resume_input(
        [
            {
                "type": "agentengine.resume_checkpoint",
                "run_id": "run-1",
                "checkpoint_id": "ckpt-1",
                "resume_attempt_id": "resume-1",
                "framework": "langgraph",
                "framework_ref": {
                    "langgraph": {
                        "thread_id": "tenant:agent:sess-1",
                        "checkpoint_id": "ckpt-1",
                    }
                },
            }
        ]
    )

    assert resume_input == {
        "type": "agentengine.resume_checkpoint",
        "run_id": "run-1",
        "checkpoint_id": "ckpt-1",
        "resume_attempt_id": "resume-1",
        "framework": "langgraph",
        "framework_ref": {
            "langgraph": {
                "thread_id": "tenant:agent:sess-1",
                "checkpoint_id": "ckpt-1",
            }
        },
    }


def test_build_runner_request_payload_exposes_invocation_id():
    prepared = PreparedConversationTurn(
        session_id="sess-1",
        invocation_id="inv-runtime-cancel",
        user_input="hello",
        user_display_input="hello",
        history=[],
        input_content=[],
        input_messages=[],
        user_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
    )
    runtime_context = PlatformInvocationContext(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        history=[],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="langgraph",
    )

    payload = _build_runner_request_payload(
        prepared=prepared,
        model="demo-model",
        runtime_context=runtime_context,
    )

    assert payload["invocation_id"] == "inv-runtime-cancel"


@pytest.mark.asyncio
async def test_invoke_conversation_once_checkpoint_resume_writes_runtime_event(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _StubRunner()
    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        messages=[],
        model="demo-model",
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        resume_input={
            "type": "agentengine.resume_checkpoint",
            "run_id": "run-1",
            "checkpoint_id": "ckpt-1",
            "resume_attempt_id": "resume-1",
            "framework": "langgraph",
            "framework_ref": {
                "langgraph": {
                    "thread_id": "tenant:agent:sess-1",
                    "checkpoint_id": "ckpt-1",
                }
            },
        },
    )

    events = await service.get_events(session_id)
    resume_events = [event for event in events if event.event_type == "run_resume"]
    assert len(resume_events) == 1
    assert resume_events[0].metadata["run_id"] == "run-1"
    assert resume_events[0].metadata["checkpoint_id"] == "ckpt-1"
    assert resume_events[0].metadata["resume_attempt_id"] == "resume-1"
    assert build_history_from_events(events) == [{"role": "model", "content": "assistant says hi"}]
    assert runner.calls[0]["checkpoint_resume"] is True
    assert runner.calls[0]["run_id"] == "run-1"
    assert runner.calls[0]["framework_ref"]["langgraph"]["checkpoint_id"] == "ckpt-1"
    assert result["metadata"]["agentengine"]["run_id"] == "run-1"
    assert result["metadata"]["agentengine"]["resume_attempt_id"] == "resume-1"


@pytest.mark.asyncio
async def test_invoke_conversation_once_failure_does_not_write_completed_or_assistant(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-fail")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _FailingRunner()
    with pytest.raises(RuntimeError, match="boom"):
        await invoke_conversation_once(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-fail",
            messages=[{"role": "user", "content": "hello"}],
            model="demo-model",
            prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        )

    events = await service.get_events("sess-fail")
    assert [event.event_type for event in events] == ["user_message", "run_status", "run_status"]
    assert [event.content.get("status") for event in events if event.event_type == "run_status"] == [
        "in_progress",
        "failed",
    ]


@pytest.mark.asyncio
async def test_checkpoint_resume_response_metadata_prefers_new_checkpoint(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _CheckpointResumeAdvancedRunner()
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        messages=[],
        model="demo-model",
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        resume_input={
            "type": "agentengine.resume_checkpoint",
            "run_id": "run-1",
            "checkpoint_id": "ckpt-before-resume",
            "resume_attempt_id": "resume-1",
            "framework": "langgraph",
            "framework_ref": {
                "langgraph": {
                    "thread_id": "tenant:agent:sess-1",
                    "checkpoint_id": "ckpt-before-resume",
                }
            },
        },
    )

    assert (
        result["metadata"]["agentengine"]["framework_ref"]["langgraph"]["checkpoint_id"]
        == "ckpt-after-resume"
    )
    events = await service.get_events("sess-1")
    assert [event.event_type for event in events if event.event_type.startswith("run_")] == [
        "run_resume",
        "run_status",
        "run_checkpoint",
        "run_status",
    ]


@pytest.mark.asyncio
async def test_invoke_conversation_once_records_runner_checkpoint_metadata(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _CheckpointMetadataRunner()
    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        messages=[{"role": "user", "content": "hello"}],
        model="demo-model",
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
    )

    events = await service.get_events(session_id)
    checkpoint_events = [event for event in events if event.event_type == "run_checkpoint"]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0].metadata["run_id"] == "run-1"
    assert checkpoint_events[0].metadata["checkpoint_id"] == "ckpt-1"
    assert checkpoint_events[0].metadata["framework_ref"]["langgraph"]["thread_id"] == "tenant:agent:sess-1"
    assert result["metadata"]["agentengine"]["framework_ref"]["langgraph"]["checkpoint_id"] == "ckpt-1"


@pytest.mark.asyncio
async def test_stream_conversation_turn_records_checkpoint_chunk(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _CheckpointMetadataStreamingRunner()
    chunks = [
        chunk
        async for chunk in stream_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-1",
            messages=[{"role": "user", "content": "hello"}],
            model="demo-model",
            prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        )
    ]

    events = await service.get_events("sess-1")
    checkpoint_events = [event for event in events if event.event_type == "run_checkpoint"]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0].metadata["run_id"] == "run-1"
    assert checkpoint_events[0].metadata["checkpoint_id"] == "ckpt-stream"
    completed = [chunk for chunk in chunks if "response.completed" in chunk][0]
    assert "ckpt-stream" in completed


@pytest.mark.asyncio
async def test_stream_conversation_turn_preserves_checkpoint_phase(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _CheckpointMetadataPhaseStreamingRunner()
    chunks = [
        chunk
        async for chunk in stream_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-1",
            messages=[{"role": "user", "content": "hello"}],
            model="demo-model",
            prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        )
    ]

    events = await service.get_events("sess-1")
    checkpoint_events = [event for event in events if event.event_type == "run_checkpoint"]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0].metadata["checkpoint_id"] == "ckpt-business-stage"
    assert checkpoint_events[0].metadata["phase"] == "数据清洗完成，等待生成报告"
    assert checkpoint_events[0].metadata["stage"] == "清洗聚合指标"
    assert checkpoint_events[0].metadata["summary"] == "GMV、转化率和退款率已经聚合完成"
    assert checkpoint_events[0].metadata["next_action"] == "恢复后继续生成复盘报告"
    assert any("response.completed" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_stream_checkpoint_resume_falls_back_to_original_run_id(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    runner = _CheckpointMetadataWithoutRunIdStreamingRunner()
    chunks = [
        chunk
        async for chunk in stream_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            session_id="sess-1",
            messages=[],
            model="demo-model",
            prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
            invocation_id="resume-attempt-1",
            resume_input={
                "type": "agentengine.resume_checkpoint",
                "run_id": "run-original",
                "checkpoint_id": "ckpt-before",
                "resume_attempt_id": "resume-attempt-1",
                "framework": "langgraph",
                "framework_ref": {
                    "langgraph": {
                        "thread_id": "tenant:agent:sess-1",
                        "checkpoint_id": "ckpt-before",
                    }
                },
            },
        )
    ]

    events = await service.get_events("sess-1")
    checkpoint_events = [event for event in events if event.event_type == "run_checkpoint"]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0].metadata["run_id"] == "run-original"
    assert checkpoint_events[0].metadata["checkpoint_id"] == "ckpt-stream-after-resume"
    assert any("response.completed" in chunk for chunk in chunks)


def test_build_history_from_events_prefers_latest_checkpoint_and_tail():
    events = [
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
            seq_id=1,
        ),
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "hi"}]},
            seq_id=2,
        ),
        SessionEvent(
            id="evt-3",
            author="demo-agent",
            event_type="context_checkpoint",
            content={
                "role": "model",
                "parts": [{"text": "Earlier conversation summary:\nuser: hello | assistant: hi"}],
            },
            seq_id=3,
            metadata={"compacted_until_seq_id": 2},
        ),
        SessionEvent(
            id="evt-4",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "follow up"}]},
            seq_id=4,
        ),
    ]

    assert build_history_from_events(events) == [
        {"role": "model", "content": "Earlier conversation summary:\nuser: hello | assistant: hi"},
        {"role": "user", "content": "follow up"},
    ]


@pytest.mark.asyncio
async def test_build_run_input_auto_compacts_old_rounds_into_checkpoint(monkeypatch):
    model_context_module = importlib.import_module("ksadk.conversations.model_context")
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-compact")
    for turn in range(5):
        await service.append_event(
            "sess-compact",
            SessionEvent(
                id=f"u-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ("x" * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )
        await service.append_event(
            "sess-compact",
            SessionEvent(
                id=f"a-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"assistant-{turn} " + ("y" * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 20)
    compact_model_metadata = {
        "id": "glm-5.1",
        "context_window_tokens": 120,
        "max_output_tokens": 1,
    }

    preview = await preview_auto_compaction(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-compact",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5.1",
        model_metadata=compact_model_metadata,
        session_service_provider=lambda: service,
    )
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-compact",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5.1",
        model_metadata=compact_model_metadata,
    )

    events = await service.get_events("sess-compact")
    assert preview.should_compact is True
    assert preview.total_estimated_tokens > 0
    assert "compaction_boundary" in [event.event_type for event in events]
    assert "context_checkpoint" in [event.event_type for event in events]
    assert prepared.compaction_triggered is True
    assert prepared.compaction_trigger == "auto"
    assert prepared.compacted_until_seq_id is not None
    assert prepared.history[0]["role"] == "model"
    assert "Earlier conversation summary:" in prepared.history[0]["content"]
    assert prepared.history[-1] == {"role": "user", "content": "follow up"}


@pytest.mark.asyncio
async def test_auto_compaction_ignores_inline_image_base64_for_context_estimation(monkeypatch):
    model_context_module = importlib.import_module("ksadk.conversations.model_context")
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-image-compact")
    large_image_data = "A" * 260_000

    for turn in range(3):
        await service.append_event(
            "sess-image-compact",
            SessionEvent(
                id=f"img-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "分析这张图片"}]},
                metadata={
                    "agent_input": json.dumps(
                        [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "分析这张图片"},
                                    {
                                        "type": "input_image",
                                        "image_url": f"data:image/png;base64,{large_image_data}",
                                    },
                                ],
                            }
                        ]
                    )
                },
                invocation_id=f"img-inv-{turn}",
            ),
        )

    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 20)
    preview = await preview_auto_compaction(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-image-compact",
        messages=[{"role": "user", "content": "继续分析"}],
        model="qwen3-vl-plus",
        model_metadata={
            "id": "qwen3-vl-plus",
            "context_window_tokens": 200_000,
            "max_output_tokens": 32_000,
        },
        session_service_provider=lambda: service,
    )

    assert preview.should_compact is False
    assert preview.total_estimated_tokens < 1_000


@pytest.mark.asyncio
async def test_build_run_input_respects_explicit_model_metadata_for_auto_compaction(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-model-metadata"
    )
    for turn in range(6):
        await service.append_event(
            "sess-model-metadata",
            SessionEvent(
                id=f"u-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ("x" * 30_000)}]},
                invocation_id=f"inv-{turn}",
            ),
        )
        await service.append_event(
            "sess-model-metadata",
            SessionEvent(
                id=f"a-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={
                    "role": "model",
                    "parts": [{"text": f"assistant-{turn} " + ("y" * 30_000)}],
                },
                invocation_id=f"inv-{turn}",
            ),
        )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    preview = await preview_auto_compaction(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-model-metadata",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5.1",
        model_metadata={
            "id": "glm-5.1",
            "context_length": "64k",
            "max_completion_tokens": "8k",
        },
        session_service_provider=lambda: service,
    )
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-model-metadata",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5.1",
        model_metadata={
            "id": "glm-5.1",
            "context_length": "64k",
            "max_completion_tokens": "8k",
        },
        session_service_provider=lambda: service,
    )

    assert preview.should_compact is True
    assert preview.auto_compact_threshold_tokens == 43000
    assert prepared.compaction_triggered is True
    assert prepared.history[0]["role"] == "model"
    assert "Earlier conversation summary:" in prepared.history[0]["content"]


@pytest.mark.asyncio
async def test_invoke_conversation_once_fetches_model_metadata_from_remote_catalog(monkeypatch):
    service = InMemorySessionService()
    runner = _StubRunner()

    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={
                "data": [
                    {
                        "id": "kimi-k2.6",
                        "architecture": {
                            "input_modalities": ["文字", "图片", "视频"],
                            "output_modalities": ["文字"],
                        },
                    }
                ]
            },
            **kwargs,
        ),
    )

    session_id, _ = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "请分析图片"}],
        model="kimi-k2.6",
        prepare_runner=lambda _runner, _model: None,
        session_service_provider=lambda: service,
    )

    assert session_id
    assert runner.calls[0]["model_metadata"]["id"] == "kimi-k2.6"
    assert runner.calls[0]["model_metadata"]["architecture"]["input_modalities"] == [
        "文字",
        "图片",
        "视频",
    ]
    assert runner.calls[0]["model_metadata"]["capabilities"]["multimodal_input_image"] is True


@pytest.mark.asyncio
async def test_invoke_conversation_once_compacts_and_retries_on_prompt_too_long(monkeypatch):
    model_context_module = importlib.import_module("ksadk.conversations.model_context")
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-ptl")
    for turn in range(4):
        await service.append_event(
            "sess-ptl",
            SessionEvent(
                id=f"u-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ("x" * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )
        await service.append_event(
            "sess-ptl",
            SessionEvent(
                id=f"a-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"assistant-{turn} " + ("y" * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(model_context_module, "DEFAULT_CONTEXT_WINDOW_TOKENS", 120)
    monkeypatch.setattr(model_context_module, "DEFAULT_MAX_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 20)
    runner = _PromptTooLongRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-ptl",
        messages=[{"role": "user", "content": "new follow up"}],
        model="gpt-4o",
        prepare_runner=lambda current, model: current.prepare_for_request(model),
    )

    assert session_id == "sess-ptl"
    assert result["output_text"] == "compacted answer"
    assert len(runner.calls) == 2
    assert len(runner.calls[1]["history"]) < len(runner.calls[0]["history"])
    assert runner.calls[1]["history"][0]["role"] == "model"
    assert "Earlier conversation summary:" in runner.calls[1]["history"][0]["content"]

    events = await service.get_events("sess-ptl")
    assert "compaction_boundary" in [event.event_type for event in events]
    assert "context_checkpoint" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_compact_conversation_history_prefers_semantic_summary_and_records_metadata(
    monkeypatch,
):
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-semantic"
    )

    for turn in range(3):
        await service.append_event(
            "sess-semantic",
            SessionEvent(
                id=f"u-sem-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"用户问题 {turn} " + ("甲" * 40)}]},
                invocation_id=f"sem-{turn}",
            ),
        )
        await service.append_event(
            "sess-semantic",
            SessionEvent(
                id=f"a-sem-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"助手回复 {turn} " + ("乙" * 40)}]},
                invocation_id=f"sem-{turn}",
            ),
        )

    class _FakeSummaryClient:
        is_available = True

        async def summarize(self, *, model, messages, timeout_ms):
            assert model == "glm-5.1"
            assert timeout_ms > 0
            assert any("当前用户目标" in item["content"] for item in messages)
            return (
                "<analysis>draft</analysis><summary>"
                "当前用户目标\n- 修复语义压缩\n\n"
                "关键约束与偏好\n- 质量优先\n\n"
                "已完成进展\n- 已生成 checkpoint\n\n"
                "重要决策/代码上下文\n- 保持 append-only 事件契约\n\n"
                "未完成事项\n- 补更多回归测试\n\n"
                "下一步工作位置\n- ksadk.conversations.runtime.compact_conversation_history"
                "</summary>",
                {"prompt_tokens": 120, "completion_tokens": 48, "total_tokens": 168},
            )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(
        "ksadk.conversations.semantic_summary.resolve_summary_model_client",
        lambda: _FakeSummaryClient(),
    )

    checkpoint = await compact_conversation_history(
        session_id="sess-semantic",
        author="demo-agent",
        invocation_id="inv-semantic",
        model="glm-5.1",
        force=True,
        keep_tail_groups=1,
        session_service_provider=lambda: service,
    )

    assert checkpoint is not None
    assert checkpoint.event_type == "context_checkpoint"
    assert "<analysis>" not in checkpoint.content["parts"][0]["text"]
    assert "当前用户目标" in checkpoint.content["parts"][0]["text"]
    assert checkpoint.metadata["summary_strategy"] == "semantic"
    assert checkpoint.metadata["summary_version"] == "v1"
    assert checkpoint.metadata["summary_model"] == "glm-5.1"
    assert checkpoint.metadata["summary_usage"]["total_tokens"] == 168


@pytest.mark.asyncio
async def test_compact_conversation_history_falls_back_to_extractive_when_semantic_summary_fails(
    monkeypatch,
):
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-fallback"
    )

    for turn in range(3):
        await service.append_event(
            "sess-fallback",
            SessionEvent(
                id=f"u-fb-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ("x" * 60)}]},
                invocation_id=f"fb-{turn}",
            ),
        )
        await service.append_event(
            "sess-fallback",
            SessionEvent(
                id=f"a-fb-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"assistant-{turn} " + ("y" * 60)}]},
                invocation_id=f"fb-{turn}",
            ),
        )

    class _BrokenSummaryClient:
        is_available = True

        async def summarize(self, *, model, messages, timeout_ms):
            raise RuntimeError("summary backend down")

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(
        "ksadk.conversations.semantic_summary.resolve_summary_model_client",
        lambda: _BrokenSummaryClient(),
    )

    checkpoint = await compact_conversation_history(
        session_id="sess-fallback",
        author="demo-agent",
        invocation_id="inv-fallback",
        model="glm-5.1",
        force=True,
        keep_tail_groups=1,
        session_service_provider=lambda: service,
    )

    assert checkpoint is not None
    assert checkpoint.metadata["summary_strategy"] == "extractive"
    assert checkpoint.metadata["summary_version"] == "v1"
    assert "summary backend down" in checkpoint.metadata["fallback_reason"]
    assert "Earlier conversation summary:" in checkpoint.content["parts"][0]["text"]


def test_plan_compaction_keeps_pending_approval_group_out_of_checkpoint():
    runtime_module = importlib.import_module("ksadk.conversations.runtime")
    events = [
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "先看第一轮"}]},
            invocation_id="inv-1",
            seq_id=1,
        ),
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "第一轮回复"}]},
            invocation_id="inv-1",
            seq_id=2,
        ),
        SessionEvent(
            id="evt-3",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "请确认是否继续执行部署"}]},
            invocation_id="inv-2",
            seq_id=3,
        ),
        SessionEvent(
            id="evt-4",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "顺便记录这个当前任务"}]},
            invocation_id="inv-3",
            seq_id=4,
        ),
    ]

    plan = runtime_module._plan_compaction(
        events,
        force=True,
        keep_tail_groups=1,
    )

    assert plan.should_compact is True
    assert [[item.seq_id for item in group] for group in plan.groups_to_compact] == [[1, 2]]
    assert plan.pinned_state["pending_approvals"]
    assert "当前任务" in plan.pinned_state["current_user_goal"]
