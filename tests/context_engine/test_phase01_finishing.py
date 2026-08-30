"""PromptProjection / capability mismatch / tokenizer provider 测试。"""

from __future__ import annotations

import os

from ksadk.context_engine.capabilities import (
    codex_context_capabilities,
    detect_capability_mismatch,
    is_capability_circuit_open,
    langgraph_context_capabilities,
    mark_capability_mismatch,
    reset_capability_circuit,
)
from ksadk.context_engine.tokenizer import (
    get_default_token_counter,
    set_default_token_counter,
)
from ksadk.prompts.compiler import PromptCompiler
from ksadk.prompts.projection import project_compiled_prompt, project_to_runner_payload
from ksadk.prompts.sources import agent_identity_section, request_instructions_section


def _compiled():
    return PromptCompiler().compile(
        [agent_identity_section("你是助手"), request_instructions_section("做X")]
    )


# ---- Prompt Projection ----


def test_project_compiled_prompt_langgraph():
    compiled = _compiled()
    res = project_compiled_prompt(
        compiled,
        runner_type="langgraph",
        integration_mode="framework_assisted",
        accounting_accuracy="estimated",
    )
    assert res.runner_type == "langgraph"
    assert "system_message" in res.projected_roles
    assert res.section_hashes  # 来自 compiled
    assert res.projection_version == "v1"


def test_project_compiled_prompt_codex_native():
    compiled = _compiled()
    res = project_compiled_prompt(
        compiled,
        runner_type="codex",
        integration_mode="native_runtime",
        accounting_accuracy="runtime_reported",
    )
    assert res.projected_roles == ("base_instructions", "thread")


def test_project_to_runner_payload_native():
    compiled = _compiled()
    payload = project_to_runner_payload(compiled, integration_mode="native_runtime")
    assert "base_instructions" in payload


def test_project_to_runner_payload_hosted():
    compiled = _compiled()
    payload = project_to_runner_payload(compiled, integration_mode="ksadk_hosted")
    assert "system_message" in payload


# ---- Capability Mismatch ----


def test_detect_capability_mismatch_owner():
    caps = langgraph_context_capabilities()
    reason = detect_capability_mismatch(declared=caps, actual_history_owner="native")
    assert reason and "history_owner" in reason


def test_detect_capability_mismatch_no_reason_when_consistent():
    caps = langgraph_context_capabilities()
    assert (
        detect_capability_mismatch(declared=caps, actual_history_owner=caps.history_owner) is None
    )


def test_detect_capability_mismatch_double_compaction():
    caps = codex_context_capabilities()
    reason = detect_capability_mismatch(declared=caps, double_compaction=True)
    assert "double_compaction" in reason


def test_capability_circuit_open_after_mark():
    reset_capability_circuit(runtime_type="codex")
    assert not is_capability_circuit_open(runtime_type="codex")
    mark_capability_mismatch(runtime_type="codex")
    assert is_capability_circuit_open(runtime_type="codex")
    reset_capability_circuit(runtime_type="codex")
    assert not is_capability_circuit_open(runtime_type="codex")


def test_capability_circuit_isolates_by_runtime_type():
    reset_capability_circuit(runtime_type="codex")
    reset_capability_circuit(runtime_type="langgraph")
    mark_capability_mismatch(runtime_type="codex")
    assert is_capability_circuit_open(runtime_type="codex")
    assert not is_capability_circuit_open(runtime_type="langgraph")  # 不影响其他 Runner
    reset_capability_circuit(runtime_type="codex")


# ---- Tokenizer provider ----


def test_default_token_counter_is_heuristic_without_tiktoken():
    set_default_token_counter(None)
    os.environ.pop("KSADK_TOKENIZER_PROVIDER", None)
    os.environ["KSADK_TOKENIZER_PROVIDER"] = "heuristic"
    try:
        c = get_default_token_counter()
        assert c.name == "heuristic_cjk_ascii"
    finally:
        os.environ.pop("KSADK_TOKENIZER_PROVIDER", None)
        set_default_token_counter(None)


def test_tokenizer_tiktoken_or_heuristic_fallback():
    set_default_token_counter(None)
    os.environ["KSADK_TOKENIZER_PROVIDER"] = "auto"
    try:
        c = get_default_token_counter()
        # tiktoken 装了就是 tiktoken，没装回退 heuristic；两者都能计数
        n = c.count_text("hello world 你好")
        assert n > 0
    finally:
        os.environ.pop("KSADK_TOKENIZER_PROVIDER", None)
        set_default_token_counter(None)
