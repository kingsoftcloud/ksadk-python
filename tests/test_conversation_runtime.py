from __future__ import annotations

import importlib
import json
import time

import httpx
import pytest

from ksadk.conversations.context import build_history_from_events
from ksadk.conversations.model_context import estimate_text_tokens
from ksadk.conversations.runtime import (
    _build_runner_ambient_contexts,
    append_context_checkpoint_event,
    build_compaction_sse_event,
    build_run_input,
    compact_conversation_history,
    extract_responses_resume_input,
    invoke_conversation_once,
    preview_auto_compaction,
    stream_conversation_turn,
    stream_responses_conversation_turn,
)
from ksadk.runtime_context import get_current_invocation_context
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


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


class _StreamingRunner(_StubRunner):
    def __init__(self):
        super().__init__()
        self.stream_calls: list[dict] = []

    async def stream(self, input_data: dict):
        self.stream_calls.append(input_data)
        yield {"type": "text", "delta": "hello"}
        yield {"type": "final", "output": "hello"}


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

    assert follow_up.attachments == message["attachments"]
    assert follow_up.attachment_results == message["attachment_results"]

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
    assert runner.calls[-1]["attachment_results"] == message["attachment_results"]


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
        prepare_runner=lambda current_runner, model: current_runner.prepare_for_request(model),
        session_service_provider=lambda: service,
    )

    assert result["output_text"] == "captured"
    assert session_id
    assert runner.calls[-1]["kb_context"] == {"formatted_text": "KB facts"}
    assert runner.calls[-1]["memory_context"] == {"formatted_text": "Memory facts"}
    assert runner.calls[-1]["platform_context"]["agent_id"] == "demo-agent"
    assert runner.calls[-1]["platform_context"]["user_id"] == "user-1"
    assert runner.calls[-1]["platform_context"]["session_id"] == session_id
    assert runner.captured_runtime_context is not None
    assert runner.captured_runtime_context.agent_id == "demo-agent"
    assert runner.captured_runtime_context.user_id == "user-1"
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
    monkeypatch.setattr(model_context_module, "DEFAULT_CONTEXT_WINDOW_TOKENS", 120)
    monkeypatch.setattr(model_context_module, "DEFAULT_MAX_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 20)

    preview = await preview_auto_compaction(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-compact",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5.1",
        session_service_provider=lambda: service,
    )
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-compact",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5.1",
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
