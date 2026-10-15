"""Engine-agnostic phase handlers (Native Harness Phase 1).

Phase handlers are pure functions that receive a PhaseContext and return a
PhaseResult or NeedInteraction. They must not import LangGraph — the
architecture guard (tests/architecture/test_harness_contract.py) enforces this.
"""
