"""Deterministic identity contract tests."""

from __future__ import annotations

import pytest

from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_part_id,
    stable_scope_id,
)


def test_identity_is_stable_across_reconstruction() -> None:
    identity = stable_item_id("adk", "inv-1", "branch-a", "event-7")
    assert identity == "item_98ab9e853875ef451f105d51"
    assert identity == stable_item_id("adk", "inv-1", "branch-a", "event-7")


def test_same_adk_response_uses_one_item_but_distinct_mutations() -> None:
    item_id = stable_item_id("adk", "inv-1", "branch-a", "event-7")
    delta_id = stable_event_id("adk", "scope-1", item_id, "item.updated", "text-0", "4", 0)
    done_id = stable_event_id("adk", "scope-1", item_id, "item.completed", "text-0", "5", 0)
    assert delta_id != done_id


def test_identity_encoding_has_a_typed_prefix_and_fixed_digest_length() -> None:
    assert stable_scope_id("codex", "thread-1", "turn-1").startswith("scope_")
    assert len(stable_scope_id("codex", "thread-1", "turn-1")) == len("scope_") + 24
    assert stable_item_id("a2a", "task-1", "artifact-1").startswith("item_")
    assert stable_part_id("langgraph", "item-1", "content-block", "text", 0) == (
        "part_d1acf734a3ea17f314177990"
    )
    assert len(stable_part_id("langgraph", "item-1", "text")) == len("part_") + 24
    assert stable_event_id(
        "a2a", "scope-1", "item-1", "item.updated", "part-0", "cursor-1", 0
    ).startswith("event_")


def test_canonical_components_do_not_have_concatenation_ambiguity() -> None:
    assert stable_item_id("adk", "ab", "c") != stable_item_id("adk", "a", "bc")


@pytest.mark.parametrize(
    ("factory", "components"),
    [
        (stable_scope_id, ("adk", "inv-1", "")),
        (stable_item_id, ("adk", "inv-1", "   ")),
        (stable_part_id, ("langgraph", "item-1", "")),
        (
            stable_event_id,
            ("adk", "scope-1", "item-1", "item.updated", "text-0", None, 0),
        ),
    ],
)
def test_empty_native_identity_components_are_rejected(factory, components) -> None:
    with pytest.raises(ValueError, match="identity component"):
        factory(*components)


def test_missing_native_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="identity component"):
        stable_item_id("adk")


def test_identity_is_not_derived_from_metadata_or_content() -> None:
    identity = stable_item_id("langgraph", "namespace", "message-1", "block-0")
    assert identity == stable_item_id("langgraph", "namespace", "message-1", "block-0")
