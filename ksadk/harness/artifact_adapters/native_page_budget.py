"""Optional host-bound capacity for a returned artifact character page.

Selection runs in the effect adapter before sealing. It never mutates storage,
receipts, permissions, or the model's declared call arguments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from ksadk.harness.native.ports import RunConfiguration, ToolDefinition, ToolRequest, ToolResult


class ArtifactPageCapacityError(ValueError):
    """A valid host binding cannot fit this declared source group."""

    def __init__(self, message, *, source_group_size=None, page_limits=()):
        super().__init__(message)
        self.source_group_size = source_group_size
        self.page_limits = tuple(page_limits)


def capacity_error_content(error: ArtifactPageCapacityError | None = None):
    if error is not None and error.source_group_size is not None:
        limits = set(error.page_limits)
        host_only = limits == {"host_payload_bytes"}
        content = {"code": "artifact_page_budget_unavailable",
                   "capacity_scope": ("host_tool_result_payload" if host_only
                                      else "original_declared_source_call_group")}
        if limits:
            content["page_limits"] = sorted(limits)
        if error.source_group_size > 1 and not host_only:
            content["next_read_shape"] = "fewer_sources_per_call_group"
        return content
    # Compatibility for old policies with no declared-group binding.
    return {"code": "artifact_page_budget_unavailable",
            "capacity_scope": "original_declared_source_call_group",
            "next_read_shape": "fewer_sources_per_call_group"}


@dataclass(frozen=True)
class ArtifactPageBudget:
    max_units: int
    max_result_units: int
    count_units: Callable[[str], int]
    max_payload_bytes: int | None = None
    source_group_size: int | None = None
    count_result_message: Callable[[dict], int] | None = None
    admit_result: Callable[[ToolResult], bool] | None = None

    def __post_init__(self):
        if not callable(self.count_units):
            raise ValueError("artifact_page_counter_unavailable")
        if self.count_result_message is not None and not callable(self.count_result_message):
            raise ValueError("artifact_page_message_counter_invalid")
        if self.admit_result is not None and not callable(self.admit_result):
            raise ValueError("artifact_page_admission_invalid")
        if self.max_payload_bytes is not None and (
            type(self.max_payload_bytes) is not int or self.max_payload_bytes < 1024
        ):
            raise ValueError("artifact_page_payload_limit_invalid")
        if self.source_group_size is not None and (
            type(self.source_group_size) is not int or self.source_group_size < 1
            or self.max_payload_bytes is None
        ):
            raise ValueError("artifact_page_feedback_binding_invalid")
        invalid = tuple(label for n, label in (
            (self.max_units, "source_group_units"), (self.max_result_units, "result_group_units")
        ) if type(n) is not int or n < 1)
        if invalid:
            raise ArtifactPageCapacityError(
                "artifact_page_budget_unavailable", source_group_size=self.source_group_size,
                page_limits=invalid,
            )

    def _cost(self, text):
        cost = self.count_units(text)
        if type(cost) is not int or cost < 0 or (text and cost == 0):
            raise ValueError("artifact_page_counter_invalid")
        return cost

    def _exceeded_limits(self, page: dict, *, check_admission=True) -> tuple[str, ...]:
        # Tool JSON is itself quoted as message text. Charge real escaped
        # source, locator and cursor metadata, not just a character count.
        envelope = json.dumps({"status": "ok", "content": page}, ensure_ascii=False)
        # ArtifactRead returns no content blocks. Include their empty framing
        # nevertheless: outer result governance measures this complete payload,
        # not just the source characters or the model-facing envelope above.
        exceeded = []
        if self._cost(page["content"]) > self.max_units:
            exceeded.append("source_group_units")
        result_cost = (self.count_result_message(page) if self.count_result_message is not None
                       else self._cost(json.dumps(envelope, ensure_ascii=False)))
        if type(result_cost) is not int or result_cost < 1:
            raise ValueError("artifact_page_counter_invalid")
        if result_cost > self.max_result_units:
            exceeded.append("result_group_units")
        if self.max_payload_bytes is not None:
            payload = json.dumps(
                {"status": "ok", "content": page, "content_blocks": []},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            if len(payload.encode("utf-8")) > self.max_payload_bytes:
                exceeded.append("host_payload_bytes")
        if (check_admission and self.admit_result is not None
                and self.admit_result(ToolResult(status="ok", content=page)) is not True):
            exceeded.append("next_request_units")
        return tuple(exceeded)

    def fits(self, page: dict, *, check_admission=True) -> bool:
        return not self._exceeded_limits(page, check_admission=check_admission)

    def unavailable(self, page: dict, *, check_admission=True) -> ArtifactPageCapacityError:
        return ArtifactPageCapacityError(
            "artifact_page_budget_unavailable", source_group_size=self.source_group_size,
            page_limits=self._exceeded_limits(page, check_admission=check_admission),
        )

    def select(self, page: dict) -> dict:
        exceeded = set(self._exceeded_limits(page))
        if not exceeded:
            return dict(page)
        text, offset = page["content"], page["offset_chars"]

        def prefix(length):
            value = {**page, "content": text[:length], "next_offset_chars": offset + length,
                     "complete": page["complete"] and length == len(text)}
            if self.source_group_size is not None:
                value["page_limits"] = sorted(exceeded)
                if self.source_group_size > 1 and exceeded.difference({"host_payload_bytes"}):
                    value["next_read_shape"] = "fewer_sources_per_call_group"
            return value

        while True:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if self.fits(prefix(middle)):
                    low = middle
                else:
                    high = middle - 1
            if not low or not self.fits(prefix(low)):
                raise self.unavailable(prefix(min(1, len(text))))
            # Feedback is part of both budgets before sealing. Its framing
            # can activate another constraint; include that cause and repack.
            # The cause set only grows and has at most four members.
            more = set(self._exceeded_limits(prefix(low + 1))) if low < len(text) else set()
            if self.source_group_size is None or more.issubset(exceeded):
                return prefix(low)
            exceeded.update(more)


@dataclass(frozen=True)
class ArtifactPagePolicy:
    digest: str
    resolve: Callable[[ToolRequest, tuple[ToolDefinition, ...]], ArtifactPageBudget]
    bind_configuration: Callable[[RunConfiguration], ArtifactPagePolicy] | None = None

    def __post_init__(self):
        if (not isinstance(self.digest, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest)):
            raise ValueError("artifact_page_policy_identity_invalid")
        if not callable(self.resolve):
            raise ValueError("artifact_page_policy_unavailable")
        if self.bind_configuration is not None and not callable(self.bind_configuration):
            raise ValueError("artifact_page_configuration_binding_invalid")


__all__ = ["ArtifactPageBudget", "ArtifactPageCapacityError", "ArtifactPagePolicy"]
