from __future__ import annotations

from ksadk.context_engine.cache_observability import (
    CacheBreakRegistry,
    diagnose_cache_break,
    get_default_cache_break_registry,
)


def test_opaque_accuracy_returns_opaque_without_inference() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="h1",
        previous_stable_prefix_hash="h0",
        usage={"cache_read_input_tokens": 500},
        accounting_accuracy="opaque",
    )
    assert d.status == "opaque"
    assert d.unexpected_break is False
    assert d.cache_read_tokens == 500  # raw 信号仍记录


def test_no_stable_prefix_returns_no_cache_info() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="",
        previous_stable_prefix_hash=None,
        usage={"cache_read_input_tokens": 100},
        accounting_accuracy="runtime_reported",
    )
    assert d.status == "no_cache_info"
    assert "no stable prefix" in d.break_reason


def test_hash_changed_is_expected_invalidation() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="h1",
        previous_stable_prefix_hash="h0",
        usage={"cache_creation_input_tokens": 500},
        accounting_accuracy="runtime_reported",
    )
    assert d.status == "expected_invalidation"
    assert d.expected_invalidation is True
    assert d.unexpected_break is False


def test_explicit_invalidation_signal_is_expected() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="h1",
        previous_stable_prefix_hash="h1",
        usage={},
        accounting_accuracy="runtime_reported",
        expected_invalidation_signal=True,
    )
    assert d.status == "expected_invalidation"


def test_stable_prefix_with_cache_read_is_cached() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="h1",
        previous_stable_prefix_hash="h1",
        usage={"cache_read_input_tokens": 800, "cache_creation_input_tokens": 0},
        accounting_accuracy="runtime_reported",
    )
    assert d.status == "cached"
    assert d.cache_read_tokens == 800


def test_stable_prefix_cache_creation_without_read_is_unexpected_break() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="h1",
        previous_stable_prefix_hash="h1",
        usage={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 600},
        accounting_accuracy="runtime_reported",
    )
    assert d.status == "unexpected_break"
    assert d.unexpected_break is True
    assert "unchanged" in d.break_reason


def test_openai_cached_tokens_field_is_extracted() -> None:
    d = diagnose_cache_break(
        stable_prefix_hash="h1",
        previous_stable_prefix_hash="h1",
        usage={"prompt_tokens_details": {"cached_tokens": 300}},
        accounting_accuracy="runtime_reported",
    )
    assert d.cache_read_tokens == 300
    assert d.status == "cached"


def test_registry_records_and_retrieves_previous_hash() -> None:
    reg = CacheBreakRegistry()
    assert reg.previous("sess") is None
    reg.record("sess", "h1")
    assert reg.previous("sess") == "h1"
    reg.record("sess", "h2")
    assert reg.previous("sess") == "h2"


def test_registry_evicts_when_over_limit() -> None:
    reg = CacheBreakRegistry(limit=2)
    reg.record("s1", "h1")
    reg.record("s2", "h2")
    reg.record("s3", "h3")
    # limit=2，第三个进来后淘汰最早的 s1。
    assert reg.previous("s1") is None
    assert reg.previous("s2") == "h2"
    assert reg.previous("s3") == "h3"


def test_default_registry_is_singleton() -> None:
    assert get_default_cache_break_registry() is get_default_cache_break_registry()
