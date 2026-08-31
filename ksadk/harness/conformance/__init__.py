"""Harness Conformance 套件入口（plan §15.2）。

Phase 0：校验器 + 合成 fixtures。Phase 1 起对真实 Engine 流执行同一套
校验（见 tests/harness/）。
"""

from ksadk.harness.conformance.contract import (
    ConformanceReport,
    ConformanceViolation,
    run_conformance_suite,
    verify_cancel_honesty,
    verify_capability_state_transitions,
    verify_event_ordering,
    verify_model_provider_policy,
    verify_secret_redaction,
    verify_start_and_terminal_event,
    verify_tool_call_pairing,
    verify_tool_reliability_honesty,
)

__all__ = [
    "ConformanceReport",
    "ConformanceViolation",
    "run_conformance_suite",
    "verify_capability_state_transitions",
    "verify_cancel_honesty",
    "verify_event_ordering",
    "verify_model_provider_policy",
    "verify_secret_redaction",
    "verify_start_and_terminal_event",
    "verify_tool_call_pairing",
    "verify_tool_reliability_honesty",
]
