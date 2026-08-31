from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.observability import token_report
from ksadk.harness.prompt_cache import PromptCacheTracker
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, LiteLLMHarnessReasoner
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


class _Reasoner(HarnessReasoner):
    async def complete(self, *, model, prompt, messages, tools):
        return HarnessReasoningTurn(
            final_text="ok",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_tokens": 60,
                "reasoning_tokens": 5,
            },
        )


def _spec(instructions: str = "stable instructions") -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://cache-agent@1",
        model=ModelBinding(profile_ref="model-profile://cache-model@1"),
        prompt=PromptSpec(instructions=instructions),
    )


def test_prompt_cache_tracker_is_scope_isolated_and_reports_breaks():
    tracker = PromptCacheTracker(max_scopes=2)
    assert tracker.observe(scope="a", stable_prompt_hash="h1").reason == "cold_start"
    assert tracker.observe(scope="a", stable_prompt_hash="h1").reason == "stable_prompt_reused"
    changed = tracker.observe(scope="a", stable_prompt_hash="h2")
    assert changed.cache_break is True
    assert changed.previous_stable_prompt_hash == "h1"
    assert tracker.observe(scope="b", stable_prompt_hash="h2").reason == "cold_start"


def test_provider_usage_details_are_normalized_without_prompt_content():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=55),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
    )
    assert LiteLLMHarnessReasoner._usage_payload(usage) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 55,
        "reasoning_tokens": 7,
    }


def test_engine_emits_stable_prompt_cache_diagnostics_and_token_report():
    engine = ManagedLangGraphEngine(reasoner=_Reasoner(), context_engine=HarnessContextEngine())

    async def run_once(spec: HarnessSpec, session_id: str):
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                agent_id="cache-agent",
                user_id="u1",
                session_id=session_id,
                input="hello",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        return [event async for event in engine.stream(handle)]

    first = asyncio.run(run_once(_spec(), "s1"))
    second = asyncio.run(run_once(_spec(), "s2"))
    changed = asyncio.run(run_once(_spec("changed instructions"), "s3"))

    first_diag = next(e for e in first if e.event_type == EventType.PROMPT_CACHE_DIAGNOSTIC)
    second_diag = next(e for e in second if e.event_type == EventType.PROMPT_CACHE_DIAGNOSTIC)
    changed_diag = next(e for e in changed if e.event_type == EventType.PROMPT_CACHE_DIAGNOSTIC)
    assert first_diag.payload["reason"] == "cold_start"
    assert second_diag.payload["reason"] == "stable_prompt_reused"
    assert changed_diag.payload["cache_break"] is True
    assert "stable instructions" not in str(first_diag.payload)

    report = token_report(second)
    assert report["actual_total_cached_tokens"] == 60
    assert report["actual_total_reasoning_tokens"] == 5
    assert report["provider_cache_hit_ratio"] == 0.6
    assert report["prompt_cache_breaks"] == 0
