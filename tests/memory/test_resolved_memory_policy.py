"""ResolvedMemoryPolicy 统一解析测试（方案 §2）。"""

from __future__ import annotations

from ksadk.memory.resolved_policy import resolve_memory_policy


def test_memory_disabled():
    p = resolve_memory_policy(
        memory_enabled=False,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert not p.enabled
    assert not p.should_recall
    assert not p.should_extract_candidates
    assert not p.should_flush


def test_memory_enabled_recall_on_write_off():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="off",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.enabled
    assert p.should_recall
    assert not p.should_extract_candidates
    assert not p.should_flush


def test_shadow_extracts_but_does_not_flush():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="shadow",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_extract_candidates
    assert not p.should_flush


def test_enabled_explicit_only():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="explicit_only",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_flush
    assert p.is_explicit_only


def test_enabled_candidate():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_flush
    assert not p.is_explicit_only


def test_recall_disabled_when_memory_enabled():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=False,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert not p.should_recall
    assert p.should_flush


def test_invalid_rollout_defaults_to_off():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="bogus",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.write_rollout == "off"
    assert not p.should_flush


def test_invalid_write_mode_defaults_to_candidate():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="bogus",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.write_mode == "candidate"
