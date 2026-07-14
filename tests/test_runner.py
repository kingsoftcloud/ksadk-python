"""Tests for the current runner contract."""

from __future__ import annotations

import base64
import os
import textwrap
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ksadk.detection import DetectionResult, FrameworkType
from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.factory import create_runner


class _StubRunner(BaseRunner):
    def __init__(self, detection_result: Any, project_dir: str):
        super().__init__(detection_result, project_dir)
        self.agent = "stub-agent"

    def load_agent(self) -> None:
        self._agent = self.agent

    async def invoke(self, input_data):
        return {"output": input_data}

    async def stream(self, input_data):
        yield {"output": input_data}


class _AsyncClosableToolset:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class _SyncClosableToolset:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _AsyncAClosableToolset:
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


class _FailingClosableToolset:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1
        raise RuntimeError("close failed")


def _install_runner_module(monkeypatch, module_path: str, class_name: str):
    fake_module = ModuleType(module_path)

    class _FrameworkRunner(_StubRunner):
        pass

    _FrameworkRunner.__name__ = class_name
    setattr(fake_module, class_name, _FrameworkRunner)
    monkeypatch.setitem(__import__("sys").modules, module_path, fake_module)
    return _FrameworkRunner


def _write_adk_project(tmp_path, source: str) -> DetectionResult:
    package_name = f"demo_agent_{uuid4().hex[:8]}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return DetectionResult(
        type=FrameworkType.ADK,
        name="demo-agent",
        entry_point=f"{package_name}/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )


def _tool_names(tools: list[Any]) -> list[str]:
    return [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]


def _write_detection(
    framework_type: FrameworkType,
    *,
    entry_point: str = "demo/agent.py",
    package_path: str = "/tmp/demo",
) -> DetectionResult:
    return DetectionResult(
        type=framework_type,
        name="demo-agent",
        entry_point=entry_point,
        package_path=package_path,
        agent_variable="root_agent",
        confidence=1.0,
    )


@pytest.mark.parametrize(
    ("framework_type", "module_path", "class_name"),
    [
        (FrameworkType.ADK, "ksadk.runners.adk_runner", "ADKRunner"),
        (FrameworkType.LANGGRAPH, "ksadk.runners.langgraph_runner", "LangGraphRunner"),
        (FrameworkType.LANGCHAIN, "ksadk.runners.langchain_runner", "LangChainRunner"),
        (FrameworkType.DEEPAGENTS, "ksadk.runners.deepagents_runner", "DeepAgentsRunner"),
    ],
)
def test_create_runner_dispatches_by_framework(
    monkeypatch,
    framework_type,
    module_path: str,
    class_name: str,
):
    expected_class = _install_runner_module(monkeypatch, module_path, class_name)
    detection = DetectionResult(
        type=framework_type,
        name="demo-agent",
        entry_point="demo/agent.py",
        package_path="/tmp/demo",
        agent_variable="root_agent",
        confidence=1.0,
    )

    runner = create_runner(detection, "/workspace/demo")

    assert isinstance(runner, expected_class)
    assert runner.detection_result == detection
    assert runner.project_dir == "/workspace/demo"


def test_create_runner_rejects_unknown_framework():
    detection = DetectionResult(
        type=FrameworkType.UNKNOWN,
        name="unknown-agent",
        entry_point="",
        package_path="",
    )

    with pytest.raises(ValueError, match="不支持的框架类型"):
        create_runner(detection, "/workspace/demo")


def test_base_runner_extracts_usage_from_langchain_message_metadata():
    detection = _write_detection(FrameworkType.LANGCHAIN)
    runner = _StubRunner(detection, "/workspace/demo")
    message = SimpleNamespace(
        content="ok",
        usage_metadata={
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "input_token_details": {"cached": 3},
            "output_token_details": {"reasoning": 2},
        },
    )

    assert runner._extract_usage({"messages": [SimpleNamespace(content="older"), message]}) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "input_token_details": {"cached": 3},
        "output_token_details": {"reasoning": 2},
    }


def test_base_runner_extracts_usage_from_openai_token_usage():
    detection = _write_detection(FrameworkType.LANGCHAIN)
    runner = _StubRunner(detection, "/workspace/demo")
    message = SimpleNamespace(
        content="ok",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 8,
                "completion_tokens": 5,
                "total_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 2},
            }
        },
    )

    assert runner._extract_usage(message) == {
        "input_tokens": 8,
        "output_tokens": 5,
        "total_tokens": 13,
        "input_token_details": {"cached": 4},
        "output_token_details": {"reasoning": 2},
    }


def test_base_runner_does_not_invent_usage_from_empty_metadata():
    detection = _write_detection(FrameworkType.LANGCHAIN)
    runner = _StubRunner(detection, "/workspace/demo")

    assert runner._extract_usage(SimpleNamespace(usage_metadata={})) == {}
    assert runner._extract_usage(SimpleNamespace(response_metadata={"token_usage": {}})) == {}


def test_base_runner_default_runtime_capabilities_are_explicitly_unsupported():
    detection = _write_detection(FrameworkType.LANGCHAIN)
    runner = _StubRunner(detection, "/workspace/demo")

    capabilities = runner.get_runtime_capabilities()

    assert capabilities["Framework"] == "langchain"
    assert capabilities["CancelRun"]["Supported"] is False
    assert capabilities["CancelRun"]["RequestResults"] == ["unsupported"]
    assert capabilities["Checkpoint"]["Supported"] is False
    assert capabilities["Checkpoint"]["Backend"] == "none"
    assert capabilities["Checkpoint"]["Durable"] is False
    assert capabilities["ResumeRun"]["Supported"] is False
    assert capabilities["ResumeRun"]["ResumeMode"] == "none"
    assert capabilities["SessionContinuity"]["Supported"] is True
    assert capabilities["SessionContinuity"]["Type"] == "semantic_replay"


def test_base_runner_runtime_capabilities_detect_cancel_override():
    class _CancellableRunner(_StubRunner):
        def request_cancel(self, invocation_id: str) -> str:
            return "accepted"

    runner = _CancellableRunner(_write_detection(FrameworkType.LANGCHAIN), "/workspace/demo")

    capabilities = runner.get_runtime_capabilities()

    assert capabilities["CancelRun"]["Supported"] is True
    assert capabilities["CancelRun"]["RequestResults"] == ["accepted", "not_found", "unsupported"]


def test_langgraph_runner_checkpoint_ref_extracts_next_node():
    from ksadk.runners.langgraph_runner import LangGraphRunner

    detection = _write_detection(FrameworkType.LANGGRAPH)
    runner = LangGraphRunner(detection, "/workspace/demo")
    state = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "sess-1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "ckpt-1",
            }
        },
        next=("fetch_sources",),
    )

    framework_ref = runner._checkpoint_ref_from_state(state)

    assert framework_ref["langgraph"]["thread_id"] == "sess-1"
    assert framework_ref["langgraph"]["checkpoint_id"] == "ckpt-1"
    assert framework_ref["langgraph"]["checkpoint_ns"] == "ns"
    assert framework_ref["langgraph"]["next_node"] == "fetch_sources"
    assert framework_ref["langgraph"]["next_nodes"] == ["fetch_sources"]


def test_adk_runner_declares_native_session_continuity_without_checkpoint_resume(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._short_term_memory = object()

    capabilities = runner.get_runtime_capabilities()

    assert capabilities["Framework"] == "adk"
    assert capabilities["SessionContinuity"]["Supported"] is True
    assert capabilities["SessionContinuity"]["Type"] == "native_session"
    assert capabilities["Checkpoint"]["Supported"] is False
    assert capabilities["ResumeRun"]["Supported"] is False
    assert capabilities["ResumeRun"]["ResumeMode"] == "forward_only"
    assert "ResumabilityConfig not enabled" in capabilities["ResumeRun"]["Reason"]

def test_adk_runner_declares_runtime_resume_when_resumable_enabled(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True

    # P1.3: Without a durable session backend, Level must degrade to "semantic"
    # because in-memory state cannot survive pod restarts.
    capabilities = runner.get_runtime_capabilities()

    assert capabilities["Framework"] == "adk"
    assert capabilities["SessionContinuity"]["Supported"] is True
    assert capabilities["SessionContinuity"]["Type"] == "adk_invocation"
    assert capabilities["SessionContinuity"]["Level"] == "semantic"
    assert "in-memory" in capabilities["SessionContinuity"]["Reason"]
    assert capabilities["Checkpoint"]["Supported"] is True
    assert capabilities["Checkpoint"]["Backend"] == "adk_invocation"
    assert capabilities["ResumeRun"]["Supported"] is True
    assert capabilities["ResumeRun"]["ResumeMode"] == "invocation_id"

    checkpoint_capability = runner.describe_checkpoint_capability()
    assert checkpoint_capability["Supported"] is True
    assert checkpoint_capability["Scope"] == "invocation"
    assert checkpoint_capability["ResumeMode"] == "invocation_id"
    assert checkpoint_capability["Durable"] is False


def test_adk_runner_reports_runtime_level_with_durable_backend(tmp_path):
    """P1.3: Level should be 'runtime' when a durable session backend is present."""
    from types import SimpleNamespace

    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True
    runner._short_term_memory = SimpleNamespace(backend="database")

    capabilities = runner.get_runtime_capabilities()

    assert capabilities["SessionContinuity"]["Level"] == "runtime"
    assert "durable" in capabilities["SessionContinuity"]["Reason"]

    checkpoint_capability = runner.describe_checkpoint_capability()
    assert checkpoint_capability["Durable"] is True
    assert checkpoint_capability["SharedAcrossPods"] is True


def test_adk_runner_no_cross_session_invocation_id_corruption(tmp_path):
    """P1.1: _last_adk_invocation_id was removed; invocation_id is per-invocation local."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))

    # The instance attribute should not exist
    assert not hasattr(runner, "_last_adk_invocation_id")


@pytest.mark.asyncio
async def test_adk_runner_resume_raises_on_missing_invocation_id(tmp_path):
    """P1.2: Resume with missing invocation_id must raise, not start a new task."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True

    with pytest.raises(ValueError, match="checkpoint_not_resumable"):
        await runner._resolve_resume_invocation_id(
            input_data={},
            session_id="test-session",
            ksadk_invocation_id="test-inv",
        )


@pytest.mark.asyncio
async def test_adk_runner_resolve_resume_from_framework_ref(tmp_path):
    """P1.2: _resolve_resume_invocation_id should find invocation_id from framework_ref."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True

    result = await runner._resolve_resume_invocation_id(
        input_data={"framework_ref": {"adk": {"invocation_id": "adk-inv-123"}}},
        session_id="test-session",
        ksadk_invocation_id="test-inv",
    )
    assert result == "adk-inv-123"


def test_adk_runner_checkpoint_metadata_includes_backend_info(tmp_path):
    """P1.3: Checkpoint metadata must include backend/scope/durable."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True

    # Create a mock event with function calls
    from types import SimpleNamespace
    mock_event = SimpleNamespace(
        id="evt-1",
        author="agent",
        content=SimpleNamespace(parts=[]),
        actions=None,
    )
    mock_event.get_function_calls = lambda: [SimpleNamespace(name="tool1", id="tc1")]

    metadata = runner._extract_checkpoint_metadata(mock_event)
    assert metadata["backend"] == "in_memory"
    assert metadata["scope"] == "invocation"
    assert metadata["durable"] is False


def test_adk_runner_single_checkpoint_per_invocation(tmp_path):
    """P1.4: _next_checkpoint_seq_for_run removed; each boundary writes an incrementing
    adk-ckpt-{seq} so the latest checkpoint always reflects the newest state."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))

    # _next_checkpoint_seq_for_run should have been removed
    assert not hasattr(runner, "_next_checkpoint_seq_for_run")


@pytest.mark.asyncio
async def test_adk_runner_checkpoint_writes_latest_boundary(tmp_path, monkeypatch):
    """Checkpoint should be written at every boundary so the latest
    recovery point is always available even if the process crashes mid-stream."""
    from types import SimpleNamespace

    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True
    runner._agent = SimpleNamespace(name="test-agent")
    runner._short_term_memory = None

    written_events = []

    async def fake_append(*, session_id, author, run_id, checkpoint_id,
                          framework, framework_ref, phase, invocation_id,
                          metadata, **kw):
        written_events.append({"metadata": metadata, "checkpoint_id": checkpoint_id})
        return SimpleNamespace(id="evt")

    monkeypatch.setattr(
        "ksadk.conversations.runtime.append_run_checkpoint_event", fake_append
    )

    def make_event(tool_name):
        ev = SimpleNamespace(
            id=f"evt-{tool_name}",
            author="agent",
            invocation_id="adk-inv-1",
            content=SimpleNamespace(parts=[]),
            actions=None,
        )
        ev.get_function_calls = lambda: [SimpleNamespace(name=tool_name, id=f"tc-{tool_name}")]
        return ev

    async def fake_events():
        for name in ["tool_a", "tool_b", "tool_c"]:
            yield make_event(name)

    wrapped = runner._collect_adk_invocation_id(
        fake_events(),
        ksadk_invocation_id="ksadk-inv-1",
        session_id="sess-1",
        checkpoint_run_id="",
    )
    results = [e async for e in wrapped]

    # All three events should pass through
    assert len(results) == 3
    # Checkpoint should be written at EVERY boundary (crash recovery)
    assert len(written_events) == 3
    # Each checkpoint gets an incrementing id
    assert written_events[0]["checkpoint_id"] == "adk-ckpt-1"
    assert written_events[1]["checkpoint_id"] == "adk-ckpt-2"
    assert written_events[2]["checkpoint_id"] == "adk-ckpt-3"
    # Each write reflects its own boundary event
    assert written_events[0]["metadata"]["tool_names"] == ["tool_a"]
    assert written_events[1]["metadata"]["tool_names"] == ["tool_b"]
    assert written_events[2]["metadata"]["tool_names"] == ["tool_c"]


@pytest.mark.asyncio
async def test_adk_runner_checkpoint_seq_continues_on_resume(tmp_path, monkeypatch):
    """Resume should continue checkpoint_seq from the existing max for the
    same run_id, not restart from 0.  Otherwise IDs collide and
    append_run_checkpoint_event silently drops the new checkpoints."""
    from types import SimpleNamespace

    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True
    runner._agent = SimpleNamespace(name="test-agent")
    runner._short_term_memory = None

    # Pre-existing checkpoints from the original invocation (run-1)
    existing_events = [
        SimpleNamespace(
            event_type="run_checkpoint",
            metadata={"run_id": "run-1", "framework": "adk",
                       "checkpoint_id": "adk-ckpt-1"},
        ),
        SimpleNamespace(
            event_type="run_checkpoint",
            metadata={"run_id": "run-1", "framework": "adk",
                       "checkpoint_id": "adk-ckpt-2"},
        ),
        SimpleNamespace(
            event_type="run_checkpoint",
            metadata={"run_id": "run-1", "framework": "adk",
                       "checkpoint_id": "adk-ckpt-3"},
        ),
    ]

    async def fake_get_events(session_id):
        return existing_events

    monkeypatch.setattr(
        "ksadk.sessions.resolve_session_service",
        lambda: SimpleNamespace(get_events=fake_get_events),
    )

    written_events = []

    async def fake_append(*, session_id, author, run_id, checkpoint_id,
                          framework, framework_ref, phase, invocation_id,
                          metadata, **kw):
        written_events.append({"checkpoint_id": checkpoint_id})
        return SimpleNamespace(id="evt")

    monkeypatch.setattr(
        "ksadk.conversations.runtime.append_run_checkpoint_event", fake_append
    )

    def make_event(tool_name):
        ev = SimpleNamespace(
            id=f"evt-{tool_name}",
            author="agent",
            invocation_id="adk-inv-1",
            content=SimpleNamespace(parts=[]),
            actions=None,
        )
        ev.get_function_calls = lambda: [
            SimpleNamespace(name=tool_name, id=f"tc-{tool_name}")
        ]
        return ev

    async def fake_events():
        for name in ["tool_d", "tool_e"]:
            yield make_event(name)

    # Simulate resume: checkpoint_run_id = original run_id
    wrapped = runner._collect_adk_invocation_id(
        fake_events(),
        ksadk_invocation_id="ksadk-resume-1",
        session_id="sess-1",
        checkpoint_run_id="run-1",
    )
    results = [e async for e in wrapped]

    assert len(results) == 2
    # New checkpoints must continue from seq=4, not restart at 1
    assert len(written_events) == 2
    assert written_events[0]["checkpoint_id"] == "adk-ckpt-4"
    assert written_events[1]["checkpoint_id"] == "adk-ckpt-5"


@pytest.mark.asyncio
async def test_adk_runner_concurrent_invocation_ids_no_cross_pollution(tmp_path):
    """P1.1: Two interleaved invocations on different sessions must not
    cross-pollute invocation_id.  The old instance-level _last_adk_invocation_id
    would cause A's checkpoint to reference B's invocation."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))

    persisted = {}

    async def fake_persist(*, session_id, ksadk_invocation_id, adk_invocation_id, **kw):
        persisted[ksadk_invocation_id] = (session_id, adk_invocation_id)

    runner._persist_invocation_mapping = fake_persist

    async def make_events(invocation_id, count):
        for i in range(count):
            ev = SimpleNamespace(
                id=f"evt-{invocation_id}-{i}",
                author="agent",
                invocation_id=invocation_id,
                content=SimpleNamespace(parts=[]),
                actions=None,
            )
            ev.get_function_calls = lambda: None
            yield ev

    # Interleave two invocations A and B on the same session.
    import asyncio

    gen_a = runner._collect_adk_invocation_id_if_present(
        make_events("adk-A", 3),
        session_id="sess-1",
        ksadk_invocation_id="ksadk-A",
    )
    gen_b = runner._collect_adk_invocation_id_if_present(
        make_events("adk-B", 3),
        session_id="sess-1",
        ksadk_invocation_id="ksadk-B",
    )

    # Drain both concurrently.
    results_a = []
    results_b = []

    async def drain(gen, out):
        async for e in gen:
            out.append(e)

    await asyncio.gather(drain(gen_a, results_a), drain(gen_b, results_b))

    # Each invocation should persist its own ADK invocation_id, not the other's.
    assert persisted["ksadk-A"] == ("sess-1", "adk-A")
    assert persisted["ksadk-B"] == ("sess-1", "adk-B")
    assert len(results_a) == 3
    assert len(results_b) == 3


@pytest.mark.asyncio
async def test_adk_runner_zero_events_resume_no_unbound_error(tmp_path):
    """P1.4 sub-issue: Resuming a completed invocation returns zero events.
    The last_event guard must prevent UnboundLocalError on the 'event' variable."""
    from types import SimpleNamespace

    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True
    runner._agent = SimpleNamespace(name="test-agent")
    runner._short_term_memory = None

    # Simulate ADK returning zero events for a completed invocation.
    async def empty_events():
        return
        yield  # pragma: no cover -- unreachable, satisfies async generator

    wrapped = runner._collect_adk_invocation_id(
        empty_events(),
        ksadk_invocation_id="ksadk-inv-1",
        session_id="sess-1",
        checkpoint_run_id="run-1",
    )

    results = [e async for e in wrapped]
    assert results == []

    # The generator should not raise — no UnboundLocalError on 'last_event'.
    # (The actual usage in invoke() uses last_event to avoid referencing
    # an unbound 'event' variable.)


@pytest.mark.asyncio
async def test_adk_runner_resume_does_not_write_duplicate_audit(tmp_path, monkeypatch):
    """P2: ADKRunner must NOT call append_run_resume_event — that's owned by
    conversation runtime.  Verify no resume event is written by the runner."""
    from types import SimpleNamespace

    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._resumable = True
    runner._agent = SimpleNamespace(name="test-agent")
    runner._short_term_memory = None

    resume_calls = []

    async def fake_append_resume(*args, **kwargs):
        resume_calls.append(kwargs)

    monkeypatch.setattr(
        "ksadk.conversations.runtime.append_run_resume_event", fake_append_resume
    )

    # Simulate events with invocation_id — checkpoint writing is fine,
    # but resume audit must NOT be written by the runner.
    async def fake_events():
        for i in range(2):
            ev = SimpleNamespace(
                id=f"evt-{i}",
                author="agent",
                invocation_id="adk-inv-1",
                content=SimpleNamespace(parts=[]),
                actions=None,
            )
            ev.get_function_calls = lambda: [SimpleNamespace(name="tool", id="tc")]
            yield ev

    # Patch checkpoint writing to avoid needing a real session service.
    async def fake_checkpoint(**kw):
        pass

    monkeypatch.setattr(
        "ksadk.conversations.runtime.append_run_checkpoint_event", fake_checkpoint
    )

    wrapped = runner._collect_adk_invocation_id(
        fake_events(),
        ksadk_invocation_id="ksadk-inv-1",
        session_id="sess-1",
        checkpoint_run_id="run-1",
    )
    results = [e async for e in wrapped]

    assert len(results) == 2
    assert resume_calls == [], "Runner must not write run_resume events (P2 fix)"


@pytest.mark.asyncio
async def test_adk_runner_invocation_map_lock_prevents_lost_update(tmp_path):
    """P1.1 sub-issue: concurrent _persist_invocation_mapping calls must not
    lose mappings.  The lock serializes the read-modify-write."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))

    # Simulate a backing store with non-atomic read-modify-write.
    store = {}

    class FakeCore:
        async def get_binding_by_session_id(self, session_id, runner_key):
            return dict(store)

        async def set_binding_by_session_id(self, session_id, runner_key, delta):
            # Simulate merge: the real service does next_state.update(delta).
            store.update(delta)

    def fake_resolve_service():
        return object()

    fake_core = FakeCore()

    import ksadk.sessions.continuity as continuity_mod

    def fake_core_factory(service):
        return fake_core

    monkeypatch_local = pytest.MonkeyPatch()
    monkeypatch_local.setattr(continuity_mod, "ConversationSessionCore", fake_core_factory)

    import ksadk.sessions as sessions_mod
    monkeypatch_local.setattr(sessions_mod, "resolve_session_service", fake_resolve_service)

    # Run two concurrent persist calls — without the lock, the second would
    # overwrite the first's entry because both read the same starting state.
    import asyncio

    await asyncio.gather(
        runner._persist_invocation_mapping(
            session_id="sess-1",
            ksadk_invocation_id="ksadk-A",
            adk_invocation_id="adk-A",
        ),
        runner._persist_invocation_mapping(
            session_id="sess-1",
            ksadk_invocation_id="ksadk-B",
            adk_invocation_id="adk-B",
        ),
    )

    # Both mappings must survive — the lock prevented the lost update.
    binding = await fake_core.get_binding_by_session_id("sess-1", "adk")
    inv_map = binding.get("invocation_map", {})
    assert inv_map.get("ksadk-A") == "adk-A"
    assert inv_map.get("ksadk-B") == "adk-B"

    monkeypatch_local.undo()


def test_langgraph_runner_declares_time_travel_resume_mode(monkeypatch):
    from ksadk.runners.langgraph_runner import LangGraphRunner

    detection = _write_detection(FrameworkType.LANGGRAPH)
    runner = LangGraphRunner(detection, "/workspace/demo")
    runner._agent = SimpleNamespace(checkpointer=object())
    monkeypatch.setenv("KSADK_CHECKPOINT_BACKEND", "postgres")

    capabilities = runner.get_runtime_capabilities()

    assert capabilities["Checkpoint"]["Supported"] is True
    assert capabilities["Checkpoint"]["Backend"] == "postgres"
    assert capabilities["ResumeRun"]["Supported"] is True
    assert capabilities["ResumeRun"]["ResumeMode"] == "time_travel"
    assert capabilities["ResumeRun"]["Reason"] == ""


def test_create_runner_uses_custom_runner_class(monkeypatch, tmp_path):
    runner_class = _install_runner_module(monkeypatch, "demo_agent.runner", "CustomRunner")
    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="agent.py",
        package_path=str(tmp_path),
        agent_variable="root_agent",
        runner_class="demo_agent.runner.CustomRunner",
        confidence=1.0,
    )

    runner = create_runner(detection, str(tmp_path))

    assert isinstance(runner, runner_class)
    assert runner.detection_result is detection
    assert runner.project_dir == str(tmp_path)


def test_create_runner_rejects_custom_runner_that_is_not_base_runner(monkeypatch, tmp_path):
    fake_module = ModuleType("demo_agent.bad_runner")

    class BadRunner:
        pass

    fake_module.BadRunner = BadRunner
    monkeypatch.setitem(__import__("sys").modules, "demo_agent.bad_runner", fake_module)
    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="agent.py",
        package_path=str(tmp_path),
        agent_variable="root_agent",
        runner_class="demo_agent.bad_runner.BadRunner",
        confidence=1.0,
    )

    with pytest.raises(TypeError, match="自定义 Runner 必须继承 BaseRunner"):
        create_runner(detection, str(tmp_path))


def test_runners_package_exports_only_create_runner():
    import ksadk.runners as runners

    assert hasattr(runners, "create_runner")
    assert set(runners.__all__) == {"BaseRunner", "create_runner"}


@pytest.mark.asyncio
async def test_base_runner_close_and_async_context_are_noops():
    detection = _write_detection(FrameworkType.LANGCHAIN)
    runner = _StubRunner(detection, "/workspace/demo")

    async with runner as active_runner:
        assert active_runner is runner

    assert await runner.close() is None


@pytest.mark.asyncio
async def test_adk_runner_close_releases_runtime_toolsets_once(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    async_close = _AsyncClosableToolset()
    sync_close = _SyncClosableToolset()
    async_aclose = _AsyncAClosableToolset()
    runner._runtime_toolsets = [async_close, sync_close, async_aclose]

    await runner.close()
    await runner.close()

    assert async_close.closed == 1
    assert sync_close.closed == 1
    assert async_aclose.closed == 1
    assert runner._runtime_toolsets == []


@pytest.mark.asyncio
async def test_adk_runner_close_continues_after_toolset_failure(tmp_path, caplog):
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    failing = _FailingClosableToolset()
    ok = _AsyncClosableToolset()
    runner._runtime_toolsets = [failing, ok]

    await runner.close()

    assert failing.closed == 1
    assert ok.closed == 1
    assert runner._runtime_toolsets == []
    assert "Failed to close runtime toolset" in caplog.text


def test_langchain_runner_prepare_for_request_reloads_agent_when_model_changes(
    monkeypatch,
    tmp_path,
):
    import ksadk.runners.langchain_runner as langchain_runner_module

    loaded_models: list[tuple[str | None, bool]] = []

    def fake_load_agent_module(
        project_dir: str, entry_point: str, agent_variable: str, *,
        force_reload: bool = False,
    ):
        loaded_models.append((os.getenv("OPENAI_MODEL_NAME"), force_reload))
        return SimpleNamespace(invoke=lambda *args, **kwargs: None), ModuleType("demo.agent")

    monkeypatch.setattr(langchain_runner_module, "load_agent_module", fake_load_agent_module)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setenv("MODEL_NAME", "glm-5.1")

    runner = langchain_runner_module.LangChainRunner(
        _write_detection(FrameworkType.LANGCHAIN),
        str(tmp_path),
    )
    runner.load_agent()
    runner.prepare_for_request("gpt-4o")

    assert loaded_models == [("glm-5.1", False), ("gpt-4o", True)]


def test_langgraph_runner_prepare_for_request_reloads_agent_when_model_changes(
    monkeypatch,
    tmp_path,
):
    import ksadk.runners.langgraph_runner as langgraph_runner_module

    loaded_models: list[tuple[str | None, bool]] = []

    def fake_load_agent_module(
        project_dir: str, entry_point: str, agent_variable: str, *,
        force_reload: bool = False,
    ):
        loaded_models.append((os.getenv("OPENAI_MODEL_NAME"), force_reload))
        return SimpleNamespace(invoke=lambda *args, **kwargs: None), ModuleType("demo.agent")

    monkeypatch.setattr(langgraph_runner_module, "load_agent_module", fake_load_agent_module)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setenv("MODEL_NAME", "glm-5.1")

    runner = langgraph_runner_module.LangGraphRunner(
        _write_detection(FrameworkType.LANGGRAPH),
        str(tmp_path),
    )
    runner.load_agent()
    runner.prepare_for_request("gpt-4o")

    assert loaded_models == [("glm-5.1", False), ("gpt-4o", True)]


def test_adk_runner_prepare_for_request_updates_explicit_model_tree(monkeypatch, tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    class FakeLiteLlm:
        def __init__(self, model: str):
            self.model = model

    child_agent = SimpleNamespace(model=FakeLiteLlm("openai/glm-5.1"), sub_agents=[])
    root_agent = SimpleNamespace(model=FakeLiteLlm("openai/glm-5.1"), sub_agents=[child_agent])

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._agent = root_agent

    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    runner.prepare_for_request("gpt-4o")

    assert root_agent.model.model == "openai/gpt-4o"
    assert child_agent.model.model == "openai/gpt-4o"
    assert os.environ["OPENAI_MODEL_NAME"] == "gpt-4o"
    assert os.environ["MODEL_NAME"] == "gpt-4o"


def test_adk_runner_prepare_for_request_restores_default_model_when_request_omits_model(
    monkeypatch,
    tmp_path,
):
    from ksadk.runners.adk_runner import ADKRunner

    class FakeLiteLlm:
        def __init__(self, model: str):
            self.model = model

    child_agent = SimpleNamespace(model=FakeLiteLlm("openai/deepseek-v3.2"), sub_agents=[])
    root_agent = SimpleNamespace(
        model=FakeLiteLlm("openai/deepseek-v3.2"),
        sub_agents=[child_agent],
    )

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._agent = root_agent
    runner._default_model_name = "deepseek-v3.2"
    runner._default_model_reference = "openai/deepseek-v3.2"
    runner._active_model_name = "openai/deepseek-v3.2"

    monkeypatch.setenv("OPENAI_MODEL_NAME", "deepseek-v3.2")
    monkeypatch.setenv("MODEL_NAME", "deepseek-v3.2")

    runner.prepare_for_request("dummy")
    assert root_agent.model.model == "openai/dummy"
    assert child_agent.model.model == "openai/dummy"

    runner.prepare_for_request(None)

    assert root_agent.model.model == "openai/deepseek-v3.2"
    assert child_agent.model.model == "openai/deepseek-v3.2"
    assert os.environ["OPENAI_MODEL_NAME"] == "deepseek-v3.2"
    assert os.environ["MODEL_NAME"] == "deepseek-v3.2"


def test_base_runner_run_server_registers_runner(monkeypatch):
    recorded: dict[str, Any] = {}

    class _DemoRunner(_StubRunner):
        pass

    fake_server_module = ModuleType("ksadk.server")
    fake_server_module.app = object()
    fake_server_module.set_runner = lambda runner: recorded.setdefault("runner", runner)

    fake_uvicorn_module = ModuleType("uvicorn")
    fake_uvicorn_module.run = lambda app, host, port: recorded.update(
        {"app": app, "host": host, "port": port}
    )

    monkeypatch.setitem(__import__("sys").modules, "ksadk.server", fake_server_module)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn_module)

    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="demo/agent.py",
        package_path="/tmp/demo",
    )
    runner = _DemoRunner(detection, "/workspace/demo")

    runner.run_server(port=9000)

    assert recorded["runner"] is runner
    assert recorded["app"] is fake_server_module.app
    assert recorded["host"] == "0.0.0.0"
    assert recorded["port"] == 9000


def test_adk_runner_load_agent_does_not_inject_legacy_sandbox_tools_by_default(
    monkeypatch, tmp_path
):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        instances: list["FakeRunner"] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            FakeRunner.instances.append(self)

    monkeypatch.delenv("KSADK_SKILLS_MODE", raising=False)
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)
    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == []
    assert len(FakeRunner.instances) == 1


def test_adk_runner_load_agent_injects_builtin_tools_when_enabled(
    monkeypatch, tmp_path
):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("KSADK_BUILTIN_TOOLS_MODE", "deferred")
    monkeypatch.setenv("KSADK_BUILTIN_TOOLS_PROFILE", "coding")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    tool_names = _tool_names(runner._agent.tools)
    assert tool_names == ["tool_search", "tool_dispatcher"]
    assert "execute_bash" not in tool_names
    assert "execute_python" not in tool_names


def test_adk_runner_injects_deferred_direct_tools_for_request(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("KSADK_BUILTIN_TOOLS_MODE", "deferred")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()
    runner.inject_deferred_tools_for_request(["read_workspace_file", "edit_workspace_file"])

    assert _tool_names(runner._agent.tools) == [
        "tool_search",
        "tool_dispatcher",
        "read_workspace_file",
        "edit_workspace_file",
    ]


def test_adk_runner_load_agent_deduplicates_existing_execute_skills(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        def execute_skills(workflow_prompt: str) -> dict:
            return {"stdout": workflow_prompt}

        def keep_tool(value: str) -> str:
            return value

        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = [keep_tool, execute_skills]
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("KSADK_SKILLS_MODE", "sandbox")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    tool_names = _tool_names(runner._agent.tools)
    assert tool_names.count("execute_skills") == 1
    assert "keep_tool" in tool_names


def test_adk_runner_load_agent_skips_skill_runtime_when_not_in_sandbox_mode(
    monkeypatch, tmp_path
):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("KSADK_SKILLS_MODE", "local")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == []


def test_adk_runner_build_adk_content_supports_inline_and_reference_attachments(
    tmp_path, monkeypatch,
):
    from ksadk.runners.adk_runner import ADKRunner

    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    archive_path = tmp_path / ".agentengine" / "ui" / "files" / "abc123.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(b"PK\x03\x04demo-zip")

    content = runner._build_adk_content(
        "请总结附件",
        [
            {
                "display_name": "notes.txt",
                "mime_type": "text/plain",
                "transport": "inline",
                "data": base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii"),
            },
            {
                "display_name": "bundle.zip",
                "mime_type": "application/zip",
                "transport": "reference",
                "file_uri": "ksadk-upload://abc123",
                "storage_path": str(archive_path),
            },
        ],
    )

    assert content.parts[0].text == "请总结附件"
    assert content.parts[1].inline_data.data == "候选人简历内容".encode("utf-8")
    assert content.parts[2].inline_data.data == b"PK\x03\x04demo-zip"


def test_adk_runner_build_adk_content_does_not_read_arbitrary_local_file_uri(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))

    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("should-not-leak", encoding="utf-8")

    content = runner._build_adk_content(
        "请分析附件",
        [
            {
                "display_name": "secret.txt",
                "mime_type": "text/plain",
                "transport": "reference",
                "file_uri": f"local:{secret_path}",
            }
        ],
    )

    assert len(content.parts) == 1
    assert content.parts[0].text == "请分析附件"


def test_adk_runner_build_adk_content_skips_images_for_text_only_models(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))

    content = runner._build_adk_content(
        "请分析这张图",
        [
            {
                "display_name": "diagram.png",
                "mime_type": "image/png",
                "transport": "inline",
                "data": base64.b64encode(b"fake-png-bytes").decode("ascii"),
            }
        ],
        model_metadata={
            "capabilities": {
                "multimodal_input_image": False,
            }
        },
    )

    assert len(content.parts) == 2
    assert content.parts[0].text == "请分析这张图"
    assert "当前模型不支持图片输入" in content.parts[1].text


@pytest.mark.asyncio
async def test_adk_runner_invoke_forwards_attachment_results_via_state_delta(tmp_path, monkeypatch):
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="demo-agent")

    captured: dict[str, Any] = {}

    class _FakeRunner:
        async def run_async(
            self, *, session_id, user_id, new_message,
            state_delta=None, run_config=None,
        ):
            captured["session_id"] = session_id
            captured["user_id"] = user_id
            captured["new_message"] = new_message
            captured["state_delta"] = state_delta
            yield SimpleNamespace(content=SimpleNamespace(parts=[types.Part(text="ok")]))

    async def _fake_ensure_session(external_session_id=None):
        return "adk-session-1"

    monkeypatch.setattr(runner, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(
        runner, "_prepare_trace_metadata",
        lambda session_id: ("", [], "", "demo-agent"),
    )
    runner._runner = _FakeRunner()

    result = await runner.invoke(
        {
            "session_id": "external-session",
            "input": "请分析附件",
            "attachments": [],
            "input_parts": [{"text": "请分析附件"}],
            "attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
            "current_attachments": [],
            "current_attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
            "has_current_files": True,
        }
    )

    assert result["output"] == "ok"
    assert captured["session_id"] == "adk-session-1"
    assert captured["state_delta"] == {
        "input_parts": [{"text": "请分析附件"}],
        "attachments": [],
        "attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
        "current_attachments": [],
        "current_attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
        "has_current_files": True,
    }


@pytest.mark.asyncio
async def test_adk_runner_invoke_extracts_usage_from_final_event(tmp_path, monkeypatch):
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="demo-agent")

    class _FakeRunner:
        async def run_async(
            self, *, session_id, user_id, new_message,
            state_delta=None, run_config=None,
        ):
            del session_id, user_id, new_message, state_delta, run_config
            yield SimpleNamespace(
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "total_tokens": 17,
                    "input_token_details": {},
                    "output_token_details": {"reasoning": 2},
                },
                content=SimpleNamespace(parts=[types.Part(text="ok")]),
            )

    async def _fake_ensure_session(external_session_id=None):
        del external_session_id
        return "adk-session-usage"

    monkeypatch.setattr(runner, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(
        runner, "_prepare_trace_metadata",
        lambda session_id: ("", [], "", "demo-agent"),
    )
    runner._runner = _FakeRunner()

    result = await runner.invoke({"session_id": "external-session", "input": "hello"})

    assert result["output"] == "ok"
    # 累积后空 input_token_details 不保留(无意义),output_token_details 有值保留
    assert result["usage"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "output_token_details": {"reasoning": 2},
    }
    # last_usage = 最后一次调用快照(单 event 时 = usage 自身)
    assert result["metadata"]["last_usage"]["input_tokens"] == 12


@pytest.mark.asyncio
async def test_adk_runner_invoke_accumulates_usage_across_events(tmp_path, monkeypatch):
    """多 event(agent loop 多次 LLM 调用)usage 累加,last_usage = 末个 event。"""
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py", agent_variable="root_agent", name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="demo-agent")

    class _FakeRunner:
        async def run_async(
            self, *, session_id, user_id, new_message,
            state_delta=None, run_config=None,
        ):
            del session_id, user_id, new_message, state_delta, run_config
            # 两次 LLM 调用(tool loop):第一次 input=4000,第二次 input=5000(含历史)
            yield SimpleNamespace(
                usage_metadata={"input_tokens": 4000, "output_tokens": 100, "total_tokens": 4100},
                content=SimpleNamespace(parts=[]),
            )
            yield SimpleNamespace(
                usage_metadata={"input_tokens": 5000, "output_tokens": 800, "total_tokens": 5800,
                                "input_token_details": {"cached": 4500}},
                content=SimpleNamespace(parts=[types.Part(text="final")]),
            )

    async def _fake_ensure_session(external_session_id=None):
        return "adk-session-accum"

    monkeypatch.setattr(runner, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(
        runner, "_prepare_trace_metadata",
        lambda session_id: ("", [], "", "demo-agent"),
    )
    runner._runner = _FakeRunner()

    result = await runner.invoke({"session_id": "external-session", "input": "hello"})

    # 累积值:input/output/total 相加,details 逐键求和
    assert result["usage"]["input_tokens"] == 9000
    assert result["usage"]["output_tokens"] == 900
    assert result["usage"]["total_tokens"] == 9900
    assert result["usage"]["input_token_details"]["cached"] == 4500
    # last_usage = 最后一次调用(窗口占用 = 末次 input)
    assert result["metadata"]["last_usage"]["input_tokens"] == 5000
    assert result["metadata"]["last_usage"]["input_token_details"]["cached"] == 4500


@pytest.mark.asyncio
async def test_adk_runner_stream_extracts_usage_details_from_final_event(tmp_path, monkeypatch):
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="demo-agent")

    class _FakeRunner:
        async def run_async(
            self, *, session_id, user_id, new_message,
            state_delta=None, run_config=None,
        ):
            del session_id, user_id, new_message, state_delta, run_config
            yield SimpleNamespace(
                partial=True,
                content=SimpleNamespace(parts=[types.Part(text="hello")]),
            )
            yield SimpleNamespace(
                usage_metadata={
                    "prompt_token_count": 12,
                    "candidates_token_count": 5,
                    "total_token_count": 17,
                    "cached_content_token_count": 4,
                    "tool_use_prompt_token_count": 3,
                    "thoughts_token_count": 2,
                },
                content=SimpleNamespace(parts=[]),
            )

    async def _fake_ensure_session(external_session_id=None):
        del external_session_id
        return "adk-session-stream-usage"

    monkeypatch.setattr(runner, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(
        runner, "_prepare_trace_metadata",
        lambda session_id: ("", [], "", "demo-agent"),
    )
    runner._runner = _FakeRunner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {"session_id": "external-session", "input": "hello"}
        )
    ]

    final = chunks[-1]
    assert final["output"] == "hello"
    assert final["type"] == "final"
    assert final["usage"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "input_token_details": {"cached": 4, "tool_use": 3},
        "output_token_details": {"reasoning": 2},
    }
    # last_usage = 最后一次调用快照
    assert final["metadata"]["last_usage"]["input_tokens"] == 12
    assert final["metadata"]["last_usage"]["input_token_details"]["cached"] == 4


@pytest.mark.asyncio
async def test_adk_runner_checkpoint_metadata_includes_resume_mode_annotation(tmp_path):
    """P1.4: checkpoint metadata must include resume_mode and only_latest_resumable
    so consumers know ADK only supports forward-only invocation resume, not
    arbitrary checkpoint rollback like LangGraph time_travel."""
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))

    event = SimpleNamespace(
        get_function_calls=lambda: [SimpleNamespace(name="tool", id="tc1")],
        content=SimpleNamespace(parts=[]),
        actions=None,
    )

    metadata = runner._extract_checkpoint_metadata(event)

    assert metadata["resume_mode"] == "invocation_id"
    assert metadata["only_latest_resumable"] is True
    assert metadata["backend"] == "in_memory"
    assert metadata["scope"] == "invocation"
    assert metadata["durable"] is False
    assert metadata["is_resumable"] is False  # in_memory -> not durable -> not resumable


def test_apply_adk_only_latest_resumable_marks_older_checkpoints():
    """P1.4: _apply_adk_only_latest_resumable should set IsResumable=False
    on older ADK checkpoints within the same RunId, keeping only the latest
    one resumable."""
    from ksadk.server.app import _apply_adk_only_latest_resumable

    checkpoints = [
        {
            "CheckpointId": "adk-ckpt-1",
            "RunId": "run-001",
            "SeqId": 1,
            "IsResumable": True,
            "Metadata": {"only_latest_resumable": True},
        },
        {
            "CheckpointId": "adk-ckpt-2",
            "RunId": "run-001",
            "SeqId": 2,
            "IsResumable": True,
            "Metadata": {"only_latest_resumable": True},
        },
        {
            "CheckpointId": "adk-ckpt-3",
            "RunId": "run-001",
            "SeqId": 3,
            "IsResumable": True,
            "Metadata": {"only_latest_resumable": True},
        },
    ]

    result = _apply_adk_only_latest_resumable(checkpoints)

    # Only the latest (seq=3) stays resumable
    assert result[0]["IsResumable"] is False
    assert result[1]["IsResumable"] is False
    assert result[2]["IsResumable"] is True

    # Older ones get the reason
    assert result[0]["ResumeDisabledReason"] == "新的恢复点已生成，此恢复点暂停恢复能力"
    assert result[1]["ResumeDisabledReason"] == "新的恢复点已生成，此恢复点暂停恢复能力"

    # Latest one has no disabled reason
    assert "ResumeDisabledReason" not in result[2] or result[2].get("ResumeDisabledReason") is None


def test_apply_adk_only_latest_resumable_separate_run_ids():
    """P1.4: Different RunIds should each keep their own latest checkpoint resumable."""
    from ksadk.server.app import _apply_adk_only_latest_resumable

    checkpoints = [
        {
            "CheckpointId": "adk-ckpt-1",
            "RunId": "run-A",
            "SeqId": 1,
            "IsResumable": True,
            "Metadata": {"only_latest_resumable": True},
        },
        {
            "CheckpointId": "adk-ckpt-1",
            "RunId": "run-B",
            "SeqId": 1,
            "IsResumable": True,
            "Metadata": {"only_latest_resumable": True},
        },
    ]

    result = _apply_adk_only_latest_resumable(checkpoints)
    # Both are the latest for their own RunId -> both stay resumable
    assert result[0]["IsResumable"] is True
    assert result[1]["IsResumable"] is True


def test_apply_adk_only_latest_resumable_skips_non_adk():
    """P1.4: Non-ADK checkpoints (no only_latest_resumable flag) should be untouched."""
    from ksadk.server.app import _apply_adk_only_latest_resumable

    checkpoints = [
        {
            "CheckpointId": "lg-ckpt-1",
            "RunId": "run-X",
            "SeqId": 1,
            "IsResumable": True,
            "Metadata": {"resume_mode": "time_travel"},  # langgraph, no only_latest flag
        },
        {
            "CheckpointId": "lg-ckpt-2",
            "RunId": "run-X",
            "SeqId": 2,
            "IsResumable": True,
            "Metadata": {"resume_mode": "time_travel"},
        },
    ]

    result = _apply_adk_only_latest_resumable(checkpoints)
    # Both should remain resumable since they're not ADK only_latest_resumable
    assert result[0]["IsResumable"] is True
    assert result[1]["IsResumable"] is True


def test_check_adk_latest_resumable_marks_non_latest():
    """P1.4: _check_adk_latest_resumable should disable a checkpoint that is
    not the latest for its RunId, based on event history."""
    from ksadk.server.app import _check_adk_latest_resumable

    checkpoint = {
        "CheckpointId": "adk-ckpt-1",
        "RunId": "run-001",
        "SeqId": 1,
        "IsResumable": True,
        "Metadata": {"only_latest_resumable": True},
    }

    # Events show a later checkpoint (seq=3) for the same run
    events = [
        SimpleNamespace(
            event_type="run_checkpoint",
            seq_id=1,
            metadata={"run_id": "run-001"},
        ),
        SimpleNamespace(
            event_type="run_checkpoint",
            seq_id=3,
            metadata={"run_id": "run-001"},
        ),
    ]

    result = _check_adk_latest_resumable(checkpoint, events)
    assert result["IsResumable"] is False
    assert result["ResumeDisabledReason"] == "新的恢复点已生成，此恢复点暂停恢复能力"


def test_check_adk_latest_resumable_keeps_latest():
    """P1.4: _check_adk_latest_resumable should keep the latest checkpoint resumable."""
    from ksadk.server.app import _check_adk_latest_resumable

    checkpoint = {
        "CheckpointId": "adk-ckpt-3",
        "RunId": "run-001",
        "SeqId": 3,
        "IsResumable": True,
        "Metadata": {"only_latest_resumable": True},
    }

    events = [
        SimpleNamespace(
            event_type="run_checkpoint",
            seq_id=1,
            metadata={"run_id": "run-001"},
        ),
        SimpleNamespace(
            event_type="run_checkpoint",
            seq_id=3,
            metadata={"run_id": "run-001"},
        ),
    ]

    result = _check_adk_latest_resumable(checkpoint, events)
    assert result["IsResumable"] is True
    assert "ResumeDisabledReason" not in result or result.get("ResumeDisabledReason") is None


def test_check_adk_latest_resumable_skips_non_adk():
    """P1.4: Non-ADK checkpoints should be passed through unchanged."""
    from ksadk.server.app import _check_adk_latest_resumable

    checkpoint = {
        "CheckpointId": "lg-ckpt-1",
        "RunId": "run-001",
        "SeqId": 1,
        "IsResumable": True,
        "Metadata": {"resume_mode": "time_travel"},
    }

    events = [
        SimpleNamespace(
            event_type="run_checkpoint",
            seq_id=5,
            metadata={"run_id": "run-001"},
        ),
    ]

    result = _check_adk_latest_resumable(checkpoint, events)
    assert result["IsResumable"] is True
