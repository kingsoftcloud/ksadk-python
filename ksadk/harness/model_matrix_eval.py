"""OpenAI-compatible model discovery and Harness compatibility smoke evaluation.

This module deliberately keeps credentials in process memory only.  Reports contain
the endpoint, model identifiers, capability results and token usage, never the API
key.  It is intended for pre-release verification against model gateways exposing
many models behind one ``/v1`` endpoint::

    python -m ksadk.harness.model_matrix_eval \
      --models glm-5.3,kimi-k3,qwen3.8-max --out report.json

Configuration follows the normal Harness contract: ``OPENAI_BASE_URL`` and
``OPENAI_API_KEY``.  The command is an explicit network E2E and is not part of the
default test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

from ksadk.harness.model_provider import safe_model_error_message
from ksadk.harness.reasoner import HarnessReasoner, LiteLLMHarnessReasoner
from ksadk.harness.tools import HarnessTool


@dataclass(frozen=True)
class DiscoveredModel:
    """Provider-neutral projection of one ``GET /models`` entry."""

    model_id: str
    owned_by: str = ""
    input_modalities: tuple[str, ...] = ()
    context_window: int | None = None


def _models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _model_from_wire(value: dict[str, Any]) -> DiscoveredModel | None:
    model_id = str(value.get("id") or "").strip()
    if not model_id:
        return None
    architecture = value.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}
    modalities = architecture.get("input_modalities") or value.get("input_modalities") or []
    if isinstance(modalities, str):
        modalities = [modalities]
    if not isinstance(modalities, list):
        modalities = []
    context_window = (
        value.get("context_window")
        or value.get("context_length")
        or value.get("max_context_length")
        or architecture.get("context_window")
    )
    return DiscoveredModel(
        model_id=model_id,
        owned_by=str(value.get("owned_by") or ""),
        input_modalities=tuple(str(item) for item in modalities if str(item).strip()),
        context_window=_positive_int(context_window),
    )


def discover_models(
    *,
    base_url: str,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[DiscoveredModel, ...]:
    """Discover models without persisting or returning the credential."""

    if not base_url.strip():
        raise ValueError("OPENAI_BASE_URL is required")
    if not api_key.strip():
        raise ValueError("OPENAI_API_KEY is required")
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(
        transport=transport,
        timeout=timeout_seconds,
        follow_redirects=True,
    ) as client:
        response = client.get(_models_url(base_url), headers=headers)
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("model endpoint returned an invalid /models payload")
    models = [item for row in rows if isinstance(row, dict) if (item := _model_from_wire(row))]
    return tuple(sorted(models, key=lambda item: item.model_id))


def select_models(
    discovered: Sequence[DiscoveredModel],
    requested: Sequence[str],
    *,
    limit: int = 4,
) -> tuple[DiscoveredModel, ...]:
    """Select explicitly requested models, or a deterministic representative sample."""

    by_id = {item.model_id: item for item in discovered}
    names = tuple(dict.fromkeys(name.strip() for name in requested if name.strip()))
    if names:
        missing = [name for name in names if name not in by_id]
        if missing:
            raise ValueError(f"requested model(s) not returned by /models: {missing}")
        return tuple(by_id[name] for name in names)
    return tuple(discovered[: max(1, limit)])


async def _probe_one(
    model: DiscoveredModel,
    *,
    reasoner_factory: Callable[[], HarnessReasoner],
) -> dict[str, Any]:
    reasoner = reasoner_factory()
    result: dict[str, Any] = {
        "model_id": model.model_id,
        "discovered": True,
        "basic_chat": False,
        "tool_calling": False,
        "usage_reported": False,
        "input_modalities": list(model.input_modalities),
        "context_window": model.context_window,
        "errors": [],
    }
    try:
        turn = await reasoner.complete(
            model=model.model_id,
            prompt="",
            messages=(
                {
                    "role": "user",
                    "content": "只回复 MODEL_MATRIX_OK，不要补充其他内容。",
                },
            ),
            tools=(),
        )
        result["basic_chat"] = "MODEL_MATRIX_OK" in (turn.final_text or "")
        result["usage_reported"] = bool(turn.usage)
        result["usage"] = dict(turn.usage or {})
        if not result["basic_chat"]:
            result["errors"].append("basic_chat_marker_missing")
    except Exception as exc:  # noqa: BLE001 - report provider compatibility
        result["errors"].append(
            f"basic_chat:{type(exc).__name__}:{safe_model_error_message(exc, limit=240)}"
        )

    async def _record_probe(arguments: dict[str, Any], call_id: str | None) -> dict[str, Any]:
        del call_id
        return {"ok": arguments.get("code") == "KSADK-42"}

    tool = HarnessTool(
        name="record_model_matrix_probe",
        description="Record the required compatibility probe code. Always call this tool.",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        handler=_record_probe,
        source="model-matrix-eval",
    )
    try:
        turn = await reasoner.complete(
            model=model.model_id,
            prompt="",
            messages=(
                {
                    "role": "user",
                    "content": (
                        "调用 record_model_matrix_probe 工具，并把 code 精确设置为 KSADK-42。"
                    ),
                },
            ),
            tools=(tool,),
        )
        result["tool_calling"] = any(
            call.name == tool.name and call.arguments.get("code") == "KSADK-42"
            for call in turn.tool_calls
        )
        if not result["tool_calling"]:
            result["errors"].append("expected_tool_call_missing")
    except Exception as exc:  # noqa: BLE001 - report provider compatibility
        result["errors"].append(
            f"tool_calling:{type(exc).__name__}:{safe_model_error_message(exc, limit=240)}"
        )
    result["passed"] = bool(result["basic_chat"] and result["tool_calling"])
    return result


async def evaluate_model_matrix(
    models: Sequence[DiscoveredModel],
    *,
    reasoner_factory: Callable[[], HarnessReasoner] = LiteLLMHarnessReasoner,
) -> dict[str, Any]:
    """Run basic chat and tool-calling probes for each selected model."""

    results = []
    for model in models:
        results.append(await _probe_one(model, reasoner_factory=reasoner_factory))
    return {
        "model_count": len(results),
        "passed_count": sum(1 for item in results if item["passed"]),
        "all_passed": bool(results) and all(item["passed"] for item in results),
        "results": results,
    }


def run_model_matrix(
    *,
    base_url: str,
    api_key: str,
    requested_models: Sequence[str] = (),
    limit: int = 4,
) -> dict[str, Any]:
    discovered = discover_models(base_url=base_url, api_key=api_key)
    selected = select_models(discovered, requested_models, limit=limit)
    old_base, old_key = os.getenv("OPENAI_BASE_URL"), os.getenv("OPENAI_API_KEY")
    os.environ["OPENAI_BASE_URL"] = base_url
    os.environ["OPENAI_API_KEY"] = api_key
    try:
        evaluated = asyncio.run(evaluate_model_matrix(selected))
    finally:
        if old_base is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = old_base
        if old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old_key
    return {
        "endpoint": base_url,
        "discovered_count": len(discovered),
        "selected_models": [item.model_id for item in selected],
        **evaluated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="ksadk.harness.model_matrix_eval")
    parser.add_argument("--models", default="", help="Comma-separated model identifiers")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    base_url = os.getenv("OPENAI_BASE_URL", "")
    api_key = os.getenv("OPENAI_API_KEY", "")
    report = run_model_matrix(
        base_url=base_url,
        api_key=api_key,
        requested_models=args.models.split(","),
        limit=args.limit,
    )
    safe = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(safe + "\n")
    print(safe)
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()


__all__ = [
    "DiscoveredModel",
    "discover_models",
    "evaluate_model_matrix",
    "run_model_matrix",
    "select_models",
]
