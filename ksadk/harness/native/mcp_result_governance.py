"""Runtime-owned MCP disclosure, with scoped artifacts and known effect status."""

from __future__ import annotations

import hashlib
import json

from ksadk.harness.mcp_result_policy import McpOffloadPolicy

from .model import ModelImageContent, ModelTextContent
from .ports import ArtifactWriteRequest, ToolModelObservation

_MIME = "application/vnd.ksadk.tool-result+json"


async def govern_mcp_disclosure(
    *,
    request,
    result,
    artifacts,
    payload,
    max_result_bytes,
    max_image_bytes,
):
    """Return an offloaded observation, or None for ordinary bounded shaping.

    Only this runtime stage may attach the typed reference. A failed artifact
    write discards the model view, never changes a known remote execution into
    an unknown/retryable operation. The canonical receipt retains its status.
    """
    from .artifact_runtime import _bounded_content

    if result.status not in {"ok", "error"}:
        return None
    policy = McpOffloadPolicy()
    # Inspect every host-selected model view as well as the transport body.
    # A small raw envelope cannot hide a sensitive typed metadata projection.
    observed = json.loads(payload)
    if result.model_content is not None:
        observed["model_content"] = result.model_content
    if result.model_observation is not None:
        observed["model_observation"] = result.model_observation.model_dump(mode="json")
    payload = json.dumps(observed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = payload.encode("utf-8")
    sensitive = policy.sensitive_match(payload)
    if not sensitive and len(encoded) <= policy.single_result_threshold_bytes:
        return None
    ref, reason = None, "artifact_store_unavailable"
    if artifacts is not None:
        try:
            usage = getattr(artifacts, "usage_bytes", None)
            used = (
                await usage(request.scope, request.run_id)
                if callable(usage)
                else sum(ref.size_bytes for ref in request.available_artifacts)
            )
            if type(used) is not int or used < 0:
                raise ValueError("invalid artifact usage")
            if used + len(encoded) > policy.run_total_quota_bytes:
                reason = "artifact_quota_exceeded"
            else:
                written = await artifacts.write(
                    ArtifactWriteRequest(
                        scope=request.scope,
                        run_id=request.run_id,
                        operation_key=request.operation_key,
                        tool_call_id=request.call.call_id,
                        tool_name=request.call.name,
                        content=payload,
                        mime_type=_MIME,
                    )
                )
                from .ports import ArtifactRef

                candidate = ArtifactRef.model_validate_json(written.model_dump_json())
                if (
                    candidate.content_hash != "sha256:" + hashlib.sha256(encoded).hexdigest()
                    or candidate.size_bytes != len(encoded)
                    or candidate.mime_type != _MIME
                ):
                    raise ValueError("artifact receipt differs from MCP result")
                ref = candidate
        except Exception:
            reason = "artifact_write_failed"
    content = {
        "code": "mcp_result_offloaded" if ref is not None else "mcp_result_withheld",
        "execution_status": result.status,
        "artifact_state": "available" if ref is not None else "unavailable",
        "original_bytes": len(encoded),
        "sensitive": bool(sensitive),
        "preview": "",
        "instruction": (
            "The sensitive result was withheld from model context."
            if sensitive
            else "The complete MCP result was saved outside model context."
        ),
    }
    if ref is not None:
        content["artifact"] = ref.model_dump(mode="json")
    else:
        content["reason"] = reason
        content["instruction"] += " Do not repeat the executed operation solely for its result."
    images = ()
    if not sensitive:
        observation = result.model_observation
        preview = (
            json.dumps(observation.content, ensure_ascii=False)
            + "\n"
            + "\n".join(
                block.text
                for block in observation.content_blocks
                if isinstance(block, ModelTextContent)
            )
            if observation is not None
            else result.model_content or payload
        )
        content["preview"] = preview[: policy.summary_chars]
        images = tuple(
            block
            for block in (
                observation.content_blocks if observation is not None else result.content_blocks
            )
            if isinstance(block, ModelImageContent)
        )
        if (
            len(images) > 8
            or sum(len(block.model_dump_json().encode()) for block in images) > max_image_bytes
        ):
            images = ()
            content["media_observation"] = {
                "image_observation_status": "not_observed",
                "instruction": "Images were not observed. Do not repeat solely for missing images.",
            }
    try:
        bounded = _bounded_content(content, result.status, max_result_bytes)
    except ValueError:
        bounded = {
            "code": "mcp_result_withheld",
            "execution_status": result.status,
            "artifact_state": "unavailable",
            "sensitive": bool(sensitive),
        }
    return result.model_copy(
        update={
            "content": result.content if result.model_observation is not None else bounded,
            "model_content": None,
            "content_blocks": (),
            "public_display": None,
            "model_observation": ToolModelObservation(
                status=result.status,
                content=bounded,
                content_blocks=images,
            ),
            "artifact_refs": (ref,) if ref is not None else (),
        }
    )
