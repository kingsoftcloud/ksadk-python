"""Optional host budget for a source page, independent of Agent frameworks."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from ksadk.conversations.model_context import estimate_text_tokens
from ksadk.tools.artifact_observation import (
    ARCHIVE_MODEL_POLICY,
    ARCHIVE_SEARCH_MODEL_POLICY,
    ARCHIVE_URI_MAX_CHARS,
    CANONICAL_ARCHIVE_MODEL_POLICY,
    OBSERVED_ARCHIVE_MODEL_POLICY,
    SOURCE_ARCHIVE_MODEL_POLICY,
    archived_observation,
)
from ksadk.toolsets.workspace_model_observation import (
    WORKSPACE_SOURCE_MODEL_VERSION,
    source_model_reply,
)

SOURCE_COUNTER_ID = "studio-cjk-ascii-estimate-125/v1"


def estimated_source_units(text: str) -> int:
    return max(1, math.ceil(estimate_text_tokens(text) * 1.25))


@dataclass(frozen=True)
class SourceReadBudget:
    max_units: int
    count_units: Callable[[str], int]
    counter_id: str
    max_result_units: int | None = None
    result_projection: str | None = None
    retain_requested_source: bool = False
    artifact_only_fallback: bool = False
    archive_model_policy: str = ARCHIVE_MODEL_POLICY

    def __post_init__(self):
        if type(self.max_units) is not int or self.max_units < 1:
            raise ValueError("source_read_budget_unavailable")
        if not callable(self.count_units) or not self.counter_id.strip():
            raise ValueError("source_read_counter_unavailable")
        if self.max_result_units is not None and (
            type(self.max_result_units) is not int or self.max_result_units < 1
        ):
            raise ValueError("source_read_budget_unavailable")
        if self.result_projection not in (None, WORKSPACE_SOURCE_MODEL_VERSION):
            raise ValueError("source_read_projection_unavailable")
        if type(self.retain_requested_source) is not bool or (
            self.retain_requested_source
            and self.result_projection != WORKSPACE_SOURCE_MODEL_VERSION
        ):
            raise ValueError("source_read_archive_policy_invalid")
        if type(self.artifact_only_fallback) is not bool or (
            self.artifact_only_fallback and not self.retain_requested_source
        ):
            raise ValueError("source_read_archive_policy_invalid")
        if self.archive_model_policy not in {
            ARCHIVE_MODEL_POLICY, ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
            OBSERVED_ARCHIVE_MODEL_POLICY, SOURCE_ARCHIVE_MODEL_POLICY,
        } or (
            self.archive_model_policy in {
                ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
                OBSERVED_ARCHIVE_MODEL_POLICY, SOURCE_ARCHIVE_MODEL_POLICY,
            }
            and not (self.retain_requested_source and self.artifact_only_fallback)
        ):
            raise ValueError("source_read_archive_policy_invalid")

    def character_limit(self, text: str, requested_limit: int) -> int:
        def fits(length):
            fragment = text[:length]
            cost = self.count_units(fragment)
            if type(cost) is not int or cost < 0 or (fragment and cost == 0):
                raise ValueError("source_read_counter_invalid")
            return cost <= self.max_units

        high = min(len(text), requested_limit)
        if fits(high):
            return max(1, high)
        low = 0
        while low < high:
            middle = (low + high + 1) // 2
            if fits(middle):
                low = middle
            else:
                high = middle - 1
        if not low:
            raise ValueError("source_read_budget_unavailable")
        return low

    def result_fits(self, result: dict) -> bool:
        if self.max_result_units is None:
            return True
        # A JSON reply is quoted inside the model tool-message text. Charge
        # that escaping as well as the page metadata, not just source text.
        text = (
            source_model_reply(result)
            if self.result_projection == WORKSPACE_SOURCE_MODEL_VERSION
            else None
        )
        if text is None:
            text = json.dumps({"status": "ok", "content": result}, ensure_ascii=False)
        if self.retain_requested_source:
            # The URI is issued only after persistence. Reserve the maximum
            # declared locator here, including its complete model framing.
            text = archived_observation(
                text,
                "artifact://" + "a" * (ARCHIVE_URI_MAX_CHARS - 11),
                policy=self.archive_model_policy,
            )
        cost = self.count_units(json.dumps(text, ensure_ascii=False))
        if type(cost) is not int or cost <= 0:
            raise ValueError("source_read_counter_invalid")
        return cost <= self.max_result_units

    def transport(self) -> dict:
        """Only a versioned host counter can cross an isolated SDK boundary."""
        if self.counter_id != SOURCE_COUNTER_ID:
            raise ValueError("source_read_counter_transport_unavailable")
        result = {
            "version": "source-read-budget/v2"
            if self.result_projection is None
            else "source-read-budget/v3",
            "counter_id": self.counter_id,
            "max_units": self.max_units,
            "max_result_units": self.max_result_units,
        }
        if self.result_projection is not None:
            result["result_projection"] = self.result_projection
        if self.retain_requested_source:
            result.update(version="source-read-budget/v4", retain_requested_source=True)
        if self.artifact_only_fallback:
            result.update(version="source-read-budget/v5", artifact_only_fallback=True)
        if self.archive_model_policy == ARCHIVE_SEARCH_MODEL_POLICY:
            result.update(
                version="source-read-budget/v6", archive_model_policy=self.archive_model_policy
            )
        if self.archive_model_policy == CANONICAL_ARCHIVE_MODEL_POLICY:
            result.update(
                version="source-read-budget/v7", archive_model_policy=self.archive_model_policy
            )
        versions = {
            OBSERVED_ARCHIVE_MODEL_POLICY: "source-read-budget/v8",
            SOURCE_ARCHIVE_MODEL_POLICY: "source-read-budget/v9",
        }
        if self.archive_model_policy in versions:
            result.update(version=versions[self.archive_model_policy],
                          archive_model_policy=self.archive_model_policy)
        return result

    @classmethod
    def from_transport(cls, value: object) -> SourceReadBudget | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("source_read_budget_transport_invalid")
        version = value.get("version")
        known = {f"source-read-budget/v{i}": i for i in range(2, 10)}
        rank = known.get(version) if isinstance(version, str) else None
        if rank is None:
            raise ValueError("source_read_budget_transport_invalid")
        expected = {"version", "counter_id", "max_units", "max_result_units"}
        if rank >= 3:
            expected.add("result_projection")
        if rank >= 4:
            expected.add("retain_requested_source")
        if rank >= 5:
            expected.add("artifact_only_fallback")
        if rank >= 6:
            expected.add("archive_model_policy")
        policies = {
            6: ARCHIVE_SEARCH_MODEL_POLICY, 7: CANONICAL_ARCHIVE_MODEL_POLICY,
            8: OBSERVED_ARCHIVE_MODEL_POLICY, 9: SOURCE_ARCHIVE_MODEL_POLICY,
        }
        if (
            set(value) != expected or value["counter_id"] != SOURCE_COUNTER_ID
            or (rank >= 3 and value["result_projection"] != WORKSPACE_SOURCE_MODEL_VERSION)
            or (rank >= 4 and value["retain_requested_source"] is not True)
            or (rank >= 5 and value["artifact_only_fallback"] is not True)
            or (rank >= 6 and value["archive_model_policy"] != policies[rank])
        ):
            raise ValueError("source_read_budget_transport_invalid")
        return cls(
            value["max_units"],
            estimated_source_units,
            SOURCE_COUNTER_ID,
            value["max_result_units"],
            value.get("result_projection"),
            value.get("retain_requested_source", False),
            value.get("artifact_only_fallback", False),
            value.get("archive_model_policy", ARCHIVE_MODEL_POLICY),
        )


_READ_BUDGET: ContextVar[SourceReadBudget | None] = ContextVar("source_read_budget", default=None)


@contextmanager
def source_read_budget_scope(budget: SourceReadBudget | None):
    token = _READ_BUDGET.set(budget)
    try:
        yield
    finally:
        _READ_BUDGET.reset(token)


def current_source_read_budget() -> SourceReadBudget | None:
    return _READ_BUDGET.get()
