"""Runtime-owned large tool result governance.

The tool effect remains successful when its observation is too large for model
context. Exact text is persisted through an injected ArtifactPort; only a
bounded preview and a typed host reference enter the model view. An explicitly
locked v3 policy also retains original content in the canonical receipt for
historical compaction. Artifact I/O stays outside reducers and never grants
completion or verification authority.
"""

from __future__ import annotations

import hashlib
import json

from ksadk.tools.artifact_observation import (
    ARCHIVE_MODEL_POLICY,
    ARCHIVE_SEARCH_MODEL_POLICY,
    CANONICAL_ARCHIVE_MODEL_POLICY,
    OBSERVED_ARCHIVE_MODEL_POLICY,
    SOURCE_ARCHIVE_MODEL_POLICY,
    archived_observation,
)
from ksadk.tools.captured_source_artifact import CAPTURED_SOURCE_MIME, encode_captured_source

from .model import ModelImageContent, ModelTextContent
from .ports import (
    ArtifactPort,
    ArtifactReadRequest,
    ArtifactRef,
    ArtifactWriteRequest,
    ToolModelObservation,
    ToolRequest,
    ToolResult,
)

_ARTIFACT_MIME = "application/vnd.ksadk.tool-result+json"
_MAX_IMAGES = 8


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text_payload(result: ToolResult) -> str:
    return _json(
        {
            "status": result.status,
            "content": result.content,
            "content_blocks": [
                block.model_dump(mode="json")
                for block in result.content_blocks
                if not isinstance(block, ModelImageContent)
            ],
        }
    )


def _preview(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    budget = max(64, min(8192, max_bytes // 3))
    if len(encoded) <= budget:
        return text
    half = max(1, budget // 2)
    return (
        encoded[:half].decode("utf-8", errors="ignore")
        + "\n…[middle omitted]…\n"
        + encoded[-half:].decode("utf-8", errors="ignore")
    )


def _valid_ref(ref: ArtifactRef, content: str, mime_type: str = _ARTIFACT_MIME) -> bool:
    encoded = content.encode("utf-8")
    return (
        ref.size_bytes == len(encoded)
        and ref.content_hash == "sha256:" + hashlib.sha256(encoded).hexdigest()
        and ref.mime_type == mime_type
    )


async def _earned_source_ref(
    artifacts: ArtifactPort, request: ToolRequest, content: str,
) -> ArtifactRef | None:
    """Reuse exact immutable source, never an execution receipt or a guessed URI.

    Each operation still keeps its own canonical ToolResult. Only its source
    locator is shared, after the bound port verifies the earned ref and every
    stored byte. Missing/corrupt/foreign artifacts fall back to a fresh write.
    """
    encoded = content.encode("utf-8")
    content_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    for ref in request.available_artifacts:
        if (ref.mime_type != CAPTURED_SOURCE_MIME or ref.size_bytes != len(encoded)
                or ref.content_hash != content_hash):
            continue
        try:
            offset = 0
            while offset < len(content):
                page = await artifacts.read(ArtifactReadRequest(
                    scope=request.scope, run_id=request.run_id, ref=ref,
                    offset_chars=offset, limit_chars=min(32000, len(content) - offset),
                ))
                end = min(offset + 32000, len(content))
                if (page.ref != ref or page.offset_chars != offset
                        or page.next_offset_chars != end
                        or page.content != content[offset:end]
                        or page.complete != (end == len(content))):
                    break
                offset = end
            else:
                return ref
        except Exception:
            # Reuse cannot grant authority or repair a damaged old artifact.
            # The new observation may still be archived normally below.
            continue
    return None


def _bounded_content(content: dict, status: str, max_bytes: int) -> dict:
    """Reserve disclosure and the locator before fitting a text preview."""

    def size(value: dict) -> int:
        # The reducer uses the ordinary JSON envelope, including its spaces.
        return len(json.dumps({"status": status, "content": value}, ensure_ascii=False).encode())

    if size(content) <= max_bytes:
        return content
    compact = {key: value for key, value in content.items() if key != "preview"}
    if "artifact" in compact:
        compact["artifact"] = {"uri": compact["artifact"]["uri"]}
        compact["instruction"] = "Use ArtifactRead to read the saved text."
    else:
        compact["instruction"] = "Use a narrower request to read the full text."
    if "media_observation" in compact:
        compact["media_observation"] = {
            "image_observation_status": "not_observed",
            "instruction": compact["media_observation"]["instruction"],
        }
    if size(compact) > max_bytes:
        # A locator which cannot fit must not be admitted as available.
        raise ValueError("artifact disclosure exceeds result budget")
    preview = content.get("preview", "")
    low, high = 0, len(preview)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = {**compact, "preview": preview[:mid] + "…"}
        if size(candidate) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return {**compact, "preview": preview[:low] + "…"} if low else compact


async def govern_tool_result(
    *,
    request: ToolRequest,
    result: ToolResult,
    artifacts: ArtifactPort | None,
    max_result_bytes: int,
    max_image_bytes: int,
) -> ToolResult:
    """Return a detached, bounded model observation with trusted refs only."""

    # ArtifactRef is runtime authority. A tool callback may return similarly
    # shaped JSON as content, but cannot add to the trusted snapshot ledger.
    clean = ToolResult.model_validate_json(
        result.model_copy(update={"artifact_refs": ()}).model_dump_json()
    )
    if request.definition.result_disclosure_policy == "mcp-default/v1":
        from .mcp_result_governance import govern_mcp_disclosure

        governed = await govern_mcp_disclosure(
            request=request, result=clean, artifacts=artifacts, payload=_text_payload(clean),
            max_result_bytes=max_result_bytes, max_image_bytes=max_image_bytes,
        )
        if governed is not None:
            return governed
    if clean.model_observation is not None:
        if request.definition.result_model_policy == clean.model_observation.version:
            return await _govern_typed_observation(
                request=request, result=clean, artifacts=artifacts,
                max_result_bytes=min(max_result_bytes, 65536), max_image_bytes=max_image_bytes,
            )
        # Missing host policy cannot grant an alternate view. Fall back to
        # governance of the original known result, without repeating its effect.
        clean = clean.model_copy(update={"model_observation": None})
    images = tuple(block for block in clean.content_blocks if isinstance(block, ModelImageContent))
    media_bytes = sum(len(_json(image.model_dump(mode="json")).encode("utf-8")) for image in images)
    media_observation = None
    if media_bytes > max_image_bytes or len(images) > _MAX_IMAGES:
        # Refuse the images, not the already observed execution fact. Turning
        # a successful mutation into an error here invites duplicate effects.
        media_observation = {
            "code": "tool_media_too_large",
            "image_observation_status": "not_observed",
            "image_count": len(images),
            "original_bytes": media_bytes,
            "limit_bytes": max_image_bytes,
            "instruction": (
                "Images were not observed (media budget). Do not repeat solely for missing "
                "images; use a smaller read-only observation."
            ),
        }
        model_content = clean.model_content
        if model_content is not None:
            try:
                view = json.loads(model_content)
            except ValueError:
                view = None
            model_content = (
                _json({**view, "media_observation": media_observation})
                if isinstance(view, dict)
                else model_content + "\n" + _json(media_observation)
            )
        clean = clean.model_copy(
            update={
                "model_content": model_content,
                "content_blocks": (
                    *(b for b in clean.content_blocks if not isinstance(b, ModelImageContent)),
                    ModelTextContent(text=_json(media_observation)),
                ),
            }
        )
        images = ()
    # A callback cannot use the alternate model view to bypass this Run's
    # observation budget when the auditable raw result happens to be small.
    # The omission disclosure above must fit this same bound.
    if clean.model_content is not None and len(clean.model_content.encode("utf-8")) > min(
        max_result_bytes, 65536
    ):
        clean = clean.model_copy(update={"model_content": None})

    payload = _text_payload(clean)
    original_bytes = len(payload.encode("utf-8"))
    archive_required = (
        request.definition.result_archive_policy
        in {
            ARCHIVE_MODEL_POLICY,
            ARCHIVE_SEARCH_MODEL_POLICY,
            CANONICAL_ARCHIVE_MODEL_POLICY,
            OBSERVED_ARCHIVE_MODEL_POLICY,
            SOURCE_ARCHIVE_MODEL_POLICY,
        }
        and clean.status == "ok"
    )
    preserve_view = archive_required and clean.model_content is not None
    archive_policy = request.definition.result_archive_policy or ARCHIVE_MODEL_POLICY
    archive_payload, archive_mime = payload, _ARTIFACT_MIME
    if archive_policy == SOURCE_ARCHIVE_MODEL_POLICY:
        source = encode_captured_source(clean.content)
        if source is not None:
            archive_payload, archive_mime = source, CAPTURED_SOURCE_MIME
    if preserve_view:
        try:
            archived_observation(clean.model_content, None, policy=archive_policy)
        except (ValueError, TypeError):
            preserve_view = False
    if original_bytes <= max_result_bytes and not archive_required:
        return clean

    def offloaded_content(ref: ArtifactRef | None) -> dict:
        content = {
            "code": "tool_result_offloaded" if ref is not None else "tool_result_truncated",
            "execution_status": clean.status,
            "original_bytes": original_bytes,
            "preview": _preview(
                clean.model_content if preserve_view else payload, max_result_bytes
            ),
            "artifact_state": "available" if ref is not None else "unavailable",
            "instruction": (
                "Use ArtifactRead with the artifact URI to read bounded pages."
                if ref is not None
                else "The full result could not be persisted; use a narrower tool request."
            ),
        }
        if ref is not None:
            content["artifact"] = ref.model_dump(mode="json")
        else:
            content["limit_bytes"] = max_result_bytes
        if media_observation is not None:
            content["media_observation"] = media_observation
        return _bounded_content(content, clean.status, max_result_bytes)

    ref: ArtifactRef | None = None
    if artifacts is not None:
        try:
            reused = (
                await _earned_source_ref(artifacts, request, archive_payload)
                if archive_mime == CAPTURED_SOURCE_MIME else None
            )
            written = reused or await artifacts.write(
                ArtifactWriteRequest(
                    scope=request.scope,
                    run_id=request.run_id,
                    operation_key=request.operation_key,
                    tool_call_id=request.call.call_id,
                    tool_name=request.call.name,
                    content=archive_payload,
                    mime_type=archive_mime,
                )
            )
            candidate = ArtifactRef.model_validate_json(written.model_dump_json())
            if not _valid_ref(candidate, archive_payload, archive_mime):
                raise ValueError("artifact receipt does not bind persisted content")
            if preserve_view:
                # Validate the complete bounded projection before admitting the
                # reference. An invalid/overlong locator must not enter context.
                projected = archived_observation(
                    clean.model_content, candidate.uri, policy=archive_policy
                )
                if len(projected.encode("utf-8")) > max_result_bytes:
                    raise ValueError("archive model view exceeds result budget")
            else:
                offloaded_content(candidate)
            ref = candidate
        except Exception:
            # ZCode's result serialization falls back to bounded truncation when
            # persistence fails. Preserve the actual execution status and never
            # leak the full payload into context.
            pass

    content = offloaded_content(ref)
    model_content = (
        archived_observation(
            clean.model_content, ref.uri if ref is not None else None, policy=archive_policy
        )
        if preserve_view
        else None
    )
    if model_content is not None and len(model_content.encode("utf-8")) > max_result_bytes:
        model_content = None
    canonical = (
        archive_policy
        in {
            CANONICAL_ARCHIVE_MODEL_POLICY,
            OBSERVED_ARCHIVE_MODEL_POLICY,
            SOURCE_ARCHIVE_MODEL_POLICY,
        }
        and clean.status == "ok"
    )
    if canonical and model_content is None:
        model_content = _json({"status": clean.status, "content": content})
    observation_digest = clean.progress_observation_digest
    if observation_digest is None and preserve_view:
        # A newly issued storage locator is not new source evidence. Compare
        # the original model observation, excluding this operation's URI.
        observation_digest = (
            "sha256:"
            + hashlib.sha256(_json(json.loads(clean.model_content)).encode("utf-8")).hexdigest()
        )
    return ToolResult(
        status=clean.status,
        progress_observation_digest=observation_digest,
        content=clean.content if canonical else content,
        model_content=model_content,
        content_blocks=clean.content_blocks if canonical else images,
        artifact_refs=(ref,) if ref is not None else (),
        resource_usage=clean.resource_usage,
        child_budget_reservation=clean.child_budget_reservation,
        child_budget_settlement=clean.child_budget_settlement,
        workspace_changed_paths=clean.workspace_changed_paths,
        browser_session_ref=clean.browser_session_ref,
        browser_session_closed=clean.browser_session_closed,
        interaction_id=clean.interaction_id,
    )


async def _govern_typed_observation(
    *, request: ToolRequest, result: ToolResult, artifacts: ArtifactPort | None,
    max_result_bytes: int, max_image_bytes: int,
) -> ToolResult:
    observation = result.model_observation
    images = tuple(b for b in observation.content_blocks if isinstance(b, ModelImageContent))
    media_bytes = sum(len(_json(b.model_dump(mode="json")).encode()) for b in images)
    warning = None
    if media_bytes > max_image_bytes or len(images) > _MAX_IMAGES:
        warning = {
            "image_observation_status": "not_observed",
            "instruction": "Images were not observed. Do not repeat solely for missing images.",
        }
        content = {"observation": observation.content, "media_observation": warning}
        observation = observation.model_copy(update={
            "content": content,
            "content_blocks": tuple(
                b for b in observation.content_blocks if not isinstance(b, ModelImageContent)
            ),
        })
        images = ()

    def text_size(value: ToolModelObservation) -> int:
        return len(json.dumps({"status": value.status, "content": value.content},
                              ensure_ascii=False).encode()) + sum(
            len(_json(b.model_dump(mode="json")).encode()) + 1
            for b in value.content_blocks if isinstance(b, ModelTextContent)
        )

    raw = _text_payload(result)
    original_bytes = len(raw.encode())
    if original_bytes <= max_result_bytes and text_size(observation) <= max_result_bytes:
        return result.model_copy(update={"model_observation": observation})

    # The saved receipt is exact. The model preview comes from the textual
    # observation, never from the raw envelope containing binary base64.
    preview_source = json.dumps(observation.content, ensure_ascii=False) + "\n" + "\n".join(
        b.text for b in observation.content_blocks if isinstance(b, ModelTextContent)
    )

    def bounded(ref: ArtifactRef | None) -> ToolModelObservation:
        content = {
            "code": "tool_result_offloaded" if ref is not None else "tool_result_truncated",
            "execution_status": result.status,
            "artifact_state": "available" if ref is not None else "unavailable",
            "original_bytes": original_bytes,
            "preview": _preview(preview_source, max_result_bytes),
            "instruction": (
                "Use ArtifactRead to read the saved result; binary JSON is not visual evidence."
                if ref is not None
                else "Full result unavailable; use a narrower read-only observation."
            ),
        }
        if ref is not None:
            content["artifact"] = ref.model_dump(mode="json")
        if warning is not None:
            content["media_observation"] = warning
        return ToolModelObservation(
            status=result.status,
            content=_bounded_content(content, result.status, max_result_bytes),
            content_blocks=images,
        )

    ref = None
    if artifacts is not None:
        try:
            written = await artifacts.write(ArtifactWriteRequest(
                scope=request.scope, run_id=request.run_id, operation_key=request.operation_key,
                tool_call_id=request.call.call_id, tool_name=request.call.name,
                content=raw, mime_type=_ARTIFACT_MIME,
            ))
            candidate = ArtifactRef.model_validate_json(written.model_dump_json())
            if not _valid_ref(candidate, raw):
                raise ValueError("artifact receipt does not bind persisted content")
            bounded(candidate)
            ref = candidate
        except Exception:
            pass
    return result.model_copy(update={
        "model_observation": bounded(ref), "artifact_refs": (ref,) if ref is not None else (),
    })


__all__ = ["govern_tool_result"]
