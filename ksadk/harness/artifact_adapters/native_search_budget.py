"""Host-bound, before-seal Search result selection. No artifact mutation."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from ksadk.harness.native.artifact_search_contract import ARTIFACT_SEARCH_DEFINITION
from ksadk.harness.native.ports import ToolResult


def digest(value):
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    )


def search_definition(policy):
    return (
        ARTIFACT_SEARCH_DEFINITION
        if policy is None
        else ARTIFACT_SEARCH_DEFINITION.model_copy(
            update={
                "feedback_profile_digest": digest(
                    {
                        "search": ARTIFACT_SEARCH_DEFINITION.feedback_profile_digest,
                        "capacity": policy.digest,
                    }
                )
            }
        )
    )


@dataclass(frozen=True)
class ArtifactSearchPolicy:
    digest: str
    resolve: Callable
    bind_configuration: Callable | None = None

    def __post_init__(self):
        if (
            not isinstance(self.digest, str)
            or len(self.digest) != 71
            or not self.digest.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in self.digest[7:])
            or not callable(self.resolve)
            or (self.bind_configuration is not None and not callable(self.bind_configuration))
        ):
            raise ValueError("artifact_search_policy_invalid")


@dataclass(frozen=True)
class ArtifactSearchBudget:
    result_message_units: int
    count_result_message: Callable
    max_payload_bytes: int

    def __post_init__(self):
        if (
            type(self.result_message_units) is not int
            or not callable(self.count_result_message)
            or type(self.max_payload_bytes) is not int
            or not 1024 <= self.max_payload_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("artifact_search_capacity_invalid")

    def fits(self, result):
        return (
            self.count_result_message(result) <= self.result_message_units
            and len(
                json.dumps(
                    {"status": result.status, "content": result.content, "content_blocks": []},
                    ensure_ascii=False,
                ).encode()
            )
            <= self.max_payload_bytes
        )

    def select(self, result):
        if self.fits(result):
            return result
        content = deepcopy(result.content)
        content["complete"] = False
        content["capacity_limited"] = True
        content["continuation"] = (
            "Complete original bytes remain at these earned URIs (ArtifactRead)."
        )

        def candidate():
            content["complete"] = all(row["complete"] for row in content["results"])
            return result.model_copy(
                update={
                    "content": deepcopy(content),
                    "progress_observation_digest": digest(content),
                }
            )

        # Keep the longest tested original excerpt prefix that fits. Each
        # candidate is measured in full; tokenizer costs need not be monotonic.
        # No excerpt is reconstructed or attributed to a different match.
        originals = [
            [m.get("excerpt", "") for m in row.get("matches", ())]
            for row in result.content["results"]
        ]
        maximum = max((len(text) for row in originals for text in row), default=0)
        lengths = []
        size = maximum // 2
        while size:
            lengths.append(size)
            size //= 2
        for size in (*lengths, 0):
            for row, texts in zip(content["results"], originals):
                for match, text in zip(row.get("matches", ()), texts):
                    if len(text) > size:
                        match["excerpt"] = text[:size]
                        match["excerpt_truncated"] = True
                        row["complete"] = False
            value = candidate()
            if self.fits(value):
                return value
        # Drop only visible matches, retaining each original URI and coordinate
        # system. Empty+complete=false is not a statement that no match exists.
        for row in reversed(content["results"]):
            while row.get("matches"):
                row["matches"].pop()
                row["complete"] = False
                value = candidate()
                if self.fits(value):
                    return value
        # The error makes no fit/coverage claim and may leave Context blocked.
        return ToolResult(status="error", content={"code": "artifact_search_capacity_unavailable"})
