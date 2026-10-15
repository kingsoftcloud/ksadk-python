"""Scoped, host-bound Context effects and their non-authorizing data projection."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, model_validator

from ksadk.plugins.context_contributor import ContextContributorResponse

from .context_contributor_contract import (
    ContextContributorPolicy,
    contributor_exchange,
    contributor_messages,
)
from .context_history import ContextRuntimeError, _goal_message_index
from .state import Contract, RunScope, RunSnapshot
from .transitions import finish

if TYPE_CHECKING:
    from .ports import RunConfiguration


def content_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def contribution_payload(response: ContextContributorResponse) -> str:
    """Canonical structured data survive Store JSON object-key ordering."""
    return json.dumps(response.model_dump(mode="json", by_alias=True),
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


class ContextSourceDefinition(Contract):
    source_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9._-]+$")
    source_ref: str = Field(min_length=1, max_length=512)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # Legacy string Contributors are data. Only the host may attest that a
    # source already belongs to the compiled prompt; then it is a receipt only.
    projection: Literal["quoted_data", "compiled_reference", "fragment_sections"] = "quoted_data"
    contributor_policy: ContextContributorPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    max_chars: int = Field(default=32_000, strict=True, ge=1, le=131_072)
    # Match the existing SDK Contributor runner's 3000 ms default. This is
    # included in the source definition, not a hidden model/tool retry limit.
    timeout_seconds: float = Field(default=3, gt=0, le=30, allow_inf_nan=False)
    failure_mode: Literal["fail", "warn", "skip"] = "fail"

    @model_validator(mode="after")
    def contributor_policy_matches_projection(self):
        if (self.projection == "fragment_sections") != (self.contributor_policy is not None):
            raise ValueError("fragment projection requires its host Contributor policy")
        if self.contributor_policy is not None and (
            self.failure_mode != self.contributor_policy.capabilities.failure_mode
            or self.timeout_seconds != self.contributor_policy.capabilities.timeout_ms / 1000
        ):
            raise ValueError("Contributor deadline and failure mode must match the host grant")
        return self


class ContextCollectionRequest(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    goal_revision: int = Field(strict=True, ge=0)
    sources: tuple[ContextSourceDefinition, ...] = Field(min_length=1, max_length=16)
    input_budget: int | None = Field(default=None, strict=True, ge=0,
                                    exclude_if=lambda value: value is None)
    model_call_cursor: int | None = Field(default=None, strict=True, ge=0,
                                         exclude_if=lambda value: value is None)


class ContextSourceResult(Contract):
    source_id: str
    source_ref: str
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["ok", "failed"]
    content: str = Field(default="", max_length=131_072)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    contribution: ContextContributorResponse | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    contribution_digest_format: Literal["canonical-json/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    counted_tokens: int | None = Field(default=None, strict=True, ge=0,
                                      exclude_if=lambda value: value is None)
    reason: Literal[
        "", "producer_failed", "producer_invalid", "source_budget_exceeded",
        "source_timeout", "binding_unavailable", "source_integrity", "source_expired",
        "collection_budget_exceeded",
    ] = ""

    @model_validator(mode="after")
    def valid_result(self):
        if self.contribution_digest_format is not None and self.contribution is None:
            raise ValueError("Contributor digest format requires structured content")
        payload = (
            contribution_payload(self.contribution)
            if self.contribution_digest_format == "canonical-json/v1"
            else self.contribution.model_dump_json(by_alias=True)
            if self.contribution is not None else self.content
        )
        if self.content_digest != content_digest(payload):
            raise ValueError("Context content digest mismatch")
        if (self.status == "ok") != (self.reason == ""):
            raise ValueError("Context result status and reason disagree")
        if self.status == "failed" and (self.content or self.contribution is not None):
            raise ValueError("failed Context source cannot supply content")
        if self.contribution is not None and (self.content or self.counted_tokens is None):
            raise ValueError("Contributor response requires separate counted, typed content")
        return self

    @classmethod
    def accepted(cls, source: ContextSourceDefinition, text: str):
        return cls(
            source_id=source.source_id, source_ref=source.source_ref,
            binding_digest=source.binding_digest, status="ok", content=text,
            content_digest=content_digest(text),
        )

    @classmethod
    def rejected(cls, source: ContextSourceDefinition, reason: str):
        return cls(
            source_id=source.source_id, source_ref=source.source_ref,
            binding_digest=source.binding_digest, status="failed",
            content_digest=content_digest(""), reason=reason,
        )

    @classmethod
    def contributed(cls, source: ContextSourceDefinition, response, counted_tokens: int):
        response = ContextContributorResponse.model_validate_json(response.model_dump_json())
        return cls(
            source_id=source.source_id, source_ref=source.source_ref,
            binding_digest=source.binding_digest, status="ok", contribution=response,
            counted_tokens=counted_tokens,
            contribution_digest_format="canonical-json/v1",
            content_digest=content_digest(contribution_payload(response)),
        )


class ContextCollectionResult(Contract):
    scope: RunScope
    run_id: str
    session_id: str
    turn_id: str
    operation_key: str
    goal_revision: int = Field(strict=True, ge=0)
    sources: tuple[ContextSourceResult, ...] = Field(max_length=16)
    input_budget: int | None = Field(default=None, strict=True, ge=0,
                                    exclude_if=lambda value: value is None)
    model_call_cursor: int | None = Field(default=None, strict=True, ge=0,
                                         exclude_if=lambda value: value is None)

    @classmethod
    def for_request(cls, request: ContextCollectionRequest, sources):
        return cls(
            **request.model_dump(exclude={"sources", "goal"}), sources=tuple(sources)
        )


class ContextPort(Protocol):
    async def collect(self, request: ContextCollectionRequest) -> ContextCollectionResult: ...


def collection_request(snapshot: RunSnapshot, config: RunConfiguration, key: str,
                       input_budget: int | None = None):
    return ContextCollectionRequest(
        scope=snapshot.scope, run_id=snapshot.run_id, session_id=snapshot.session_id,
        turn_id=snapshot.turn_id, operation_key=key, goal=snapshot.goal,
        goal_revision=snapshot.goal_revision, sources=config.context_sources,
        input_budget=input_budget,
        model_call_cursor=(snapshot.model_calls if input_budget is not None else None),
    )


def validate_collection(request: ContextCollectionRequest, result: ContextCollectionResult,
                        *, count_message=None):
    # Revalidation includes nested content digests, even after model_copy or
    # model_construct. Neither the callback nor its retained objects own state.
    result = ContextCollectionResult.model_validate_json(result.model_dump_json())
    identity = ("scope", "run_id", "session_id", "turn_id", "operation_key", "goal_revision",
                "input_budget", "model_call_cursor")
    if any(getattr(request, key) != getattr(result, key) for key in identity):
        raise ValueError("Context receipt identity mismatch")
    ids = [item.source_id for item in result.sources]
    if len(set(ids)) != len(ids) or set(ids) != {item.source_id for item in request.sources}:
        raise ValueError("Context source receipt set mismatch")
    by_id = {item.source_id: item for item in result.sources}
    remaining = request.input_budget
    for definition in request.sources:
        item = by_id[definition.source_id]
        if (item.source_ref, item.binding_digest) != (
            definition.source_ref, definition.binding_digest
        ):
            raise ValueError("Context source binding mismatch")
        if len(item.content) > definition.max_chars:
            raise ValueError("Context source exceeds bound character budget")
        if (remaining is not None and count_message is not None
                and item.reason != "collection_budget_exceeded"):
            frames = (contributor_messages(request, definition, item.contribution)
                      if item.contribution is not None
                      else quoted_context_messages(request, (item,)))
            actual = sum(count_message(frame) for frame in frames)
            if actual != (item.counted_tokens or 0):
                raise ValueError("Context receipt count differs from the Kernel projection")
        if definition.contributor_policy is None:
            if item.contribution is not None:
                raise ValueError("string source cannot supply Contributor authority")
        elif item.status == "ok":
            if item.contribution is None or remaining is None or item.counted_tokens is None:
                raise ValueError("Contributor response has no host request budget")
            contributor_exchange(request, definition, item.contribution, remaining)
            if (len(item.contribution.model_dump_json(by_alias=True)) > definition.max_chars
                    or item.counted_tokens > min(
                        remaining, definition.contributor_policy.capabilities.max_tokens,
                    )):
                raise ValueError("Contributor response exceeds the counted host budget")
        if remaining is not None:
            used = item.counted_tokens or 0
            if used > remaining:
                raise ValueError("Context collection exceeds the host input budget")
            remaining -= used
    # Host definition order is the deterministic projection order.
    return result.model_copy(update={
        "sources": tuple(by_id[definition.source_id] for definition in request.sources)
    })


def quoted_context_messages(request: ContextCollectionRequest, sources):
    """The same non-authorizing model frames used for admission and reduction.

    Keep the old string-only aggregate unchanged. With a shared Contributor
    budget, use one attributed frame per source, so collection can debit the
    complete envelope before granting the next producer its remaining quota.
    Counted costs belong to the receipt, not recursively inside their payload.
    """
    definitions = {item.source_id: item for item in request.sources}
    data = [item.model_dump(mode="json", exclude={"counted_tokens"}) for item in sources
            if (definitions[item.source_id].projection == "quoted_data"
                or (item.status != "ok"
                    and definitions[item.source_id].failure_mode == "warn"))
            and (item.content or item.status != "ok")]
    groups = ([item] for item in data) if request.input_budget is not None else [data]
    messages = []
    for group in groups:
        if not group:
            continue
        message = {
            "role": "user", "kind": "context_contribution",
            "goal_revision": request.goal_revision,
            "content": json.dumps({
                "type": "ksadk.context-source-data/v1", "trust": "resource",
                "turn_id": request.turn_id, "goal_revision": request.goal_revision,
                "operation_key": request.operation_key, "sources": group,
            }, ensure_ascii=False, sort_keys=True),
        }
        if request.input_budget is not None:
            message["context_operation_key"] = request.operation_key
            message["context_source_id"] = group[0]["source_id"]
        messages.append(message)
    return tuple(messages)


def reduce_collection(snapshot: RunSnapshot, config: RunConfiguration, key: str, raw: dict,
                      input_budget: int | None = None, *, count_message=None):
    try:
        request = collection_request(snapshot, config, key, input_budget)
        result = validate_collection(request, ContextCollectionResult.model_validate(raw),
                                     count_message=count_message)
    except (ValueError, TypeError):
        return finish(snapshot, "unverified", "context_source_protocol_invalid")
    definitions = {item.source_id: item for item in request.sources}
    if any(
        item.status == "failed" and (
            definitions[item.source_id].failure_mode == "fail"
            or item.reason in {"binding_unavailable", "source_integrity",
                               "collection_budget_exceeded"}
        )
        for item in result.sources
    ):
        return finish(snapshot, "blocked", "context_source_unavailable")
    messages = snapshot.messages
    additions = []
    if request.input_budget is None:
        additions.extend(quoted_context_messages(request, result.sources))
    else:
        for item in result.sources:
            if item.contribution is not None:
                additions.extend(contributor_messages(request, definitions[item.source_id],
                                                       item.contribution))
            else:
                additions.extend(quoted_context_messages(request, (item,)))
    if additions:
        # Keep the actual user goal after these quoted data. Do not replace a
        # transcript prefix sealed by an earlier compaction or Session turn.
        try:
            index = _goal_message_index(messages, snapshot.goal)
        except ContextRuntimeError:
            return finish(snapshot, "unverified", "context_goal_message_missing")
        prefix = snapshot.compaction_history.get("prefix_message_count", 0)
        if snapshot.compaction_history.get("count", 0) and index < prefix:
            # A fresh source cannot insert into a compacted prefix. Model
            # projection orders these live frames before the retained goal;
            # the authoritative transcript and its sealed digest stay intact.
            messages = (*messages, *additions)
        else:
            messages = (*messages[:index], *additions, *messages[index:])
    return snapshot.model_copy(update={
        "messages": messages, "context_source_goal_revision": snapshot.goal_revision,
        "context_source_receipt": result.model_dump(mode="json"),
        "pending_intent": None,
    })
