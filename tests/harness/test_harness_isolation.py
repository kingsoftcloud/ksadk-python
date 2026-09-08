from __future__ import annotations

import asyncio

import pytest

from ksadk.harness import HarnessApp, HarnessConfig
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.runtime import StartRequest


class _BarrierReasoner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.ready = asyncio.Event()

    async def complete(self, *, model, prompt, messages, tools):
        del tools
        user_input = str(messages[1]["content"])
        self.calls.append((model, prompt, user_input))
        if len(self.calls) == 2:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=2)
        return HarnessReasoningTurn(final_text=f"{model}|{prompt}|{user_input}")


@pytest.mark.asyncio
async def test_per_invocation_overrides_do_not_mutate_shared_adapter(tmp_path):
    reasoner = _BarrierReasoner()
    harness = HarnessApp(
        HarnessConfig(model="base", prompt="base-prompt"),
        reasoner=reasoner,
        workspace_root=tmp_path,
    )
    adapter = harness.adapter()
    assert isinstance(adapter, HarnessRuntimeAdapter)
    first, second = await asyncio.gather(
        adapter.execute_request(
            StartRequest(
                input="one",
                user_id="u",
                session_id="one",
                metadata={"model_override": "model-a", "prompt_override": "prompt-a"},
            )
        ),
        adapter.execute_request(
            StartRequest(
                input="two",
                user_id="u",
                session_id="two",
                metadata={"model_override": "model-b", "prompt_override": "prompt-b"},
            )
        ),
    )
    assert first["output"] == "model-a|prompt-a|one"
    assert second["output"] == "model-b|prompt-b|two"
    assert harness.config.model == "base"
    assert harness.config.prompt == "base-prompt"


@pytest.mark.asyncio
async def test_two_harness_apps_own_distinct_adapter_and_sandbox_state(tmp_path):
    first_reasoner = _BarrierReasoner()
    second_reasoner = _BarrierReasoner()
    first_reasoner.ready.set()
    second_reasoner.ready.set()
    first = HarnessApp(
        HarnessConfig(model="first", prompt="first-prompt"),
        reasoner=first_reasoner,
        workspace_root=tmp_path / "first",
    )
    second = HarnessApp(
        HarnessConfig(model="second", prompt="second-prompt"),
        reasoner=second_reasoner,
        workspace_root=tmp_path / "second",
    )

    first_app = first.build_app()
    second_app = second.build_app()
    first_adapter = first.adapter()
    second_adapter = second.adapter()
    assert isinstance(first_adapter, HarnessRuntimeAdapter)
    assert isinstance(second_adapter, HarnessRuntimeAdapter)

    assert first_adapter is not second_adapter
    assert first_adapter.sandbox is not second_adapter.sandbox
    assert first_adapter.workspace_root != second_adapter.workspace_root
    assert first_app.state.runtime.stream_registry is not second_app.state.runtime.stream_registry
    assert first_app.state.runtime.sandbox_registry is not second_app.state.runtime.sandbox_registry


def test_default_workspaces_are_per_app():
    first = HarnessApp(HarnessConfig(model="first", prompt="p"))
    second = HarnessApp(HarnessConfig(model="second", prompt="p"))
    assert first.workspace_root != second.workspace_root
