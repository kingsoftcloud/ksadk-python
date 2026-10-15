"""Executable compatibility matrix for KSY Gateway and BYO endpoints.

Run explicitly because probes consume model quota::

    python -m ksadk.model_gateway.matrix \
      --endpoint https://example.invalid/v1/chat/completions \
      --models model-a,model-b --declare-out capabilities.json

The report contains no credential or raw provider body.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ksadk.harness.model_capability import (
    ModelCapabilityStore,
    apply_matrix_report,
)
from ksadk.model_gateway import ModelGatewayClient
from ksadk.model_gateway.events import ModelStreamEventType, classify_gateway_error
from ksadk.studio.contracts import ModelParameters, NetworkPolicy, ResolvedModel
from ksadk.studio.model_client import CredentialResolver

_ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQAN"
    "HQEDgslx/wAAAABJRU5ErkJggg=="
)


@dataclass
class ProbeResult:
    model_id: str
    available: bool | None = None
    basic_chat: bool | None = None
    tool_calling: bool | None = None
    stream_text: bool | None = None
    stream_tool_calling: bool | None = None
    usage_reported: bool | None = None
    reasoning: bool | None = None
    long_context: bool | None = None
    multimodal: bool | None = None
    errors: dict[str, str] | None = None


class ModelCompatibilityMatrix:
    """Run one deterministic probe suite against several bound models."""

    def __init__(
        self,
        client: ModelGatewayClient,
        *,
        endpoint: str,
        credential_ref: str,
        gateway_id: str = "ksyun",
        timeout_seconds: int = 90,
        long_context_chars: int = 0,
        probe_multimodal: bool = True,
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        self.credential_ref = credential_ref
        self.gateway_id = gateway_id
        self.timeout_seconds = timeout_seconds
        self.long_context_chars = max(0, long_context_chars)
        self.probe_multimodal = probe_multimodal
        self.network_policy = NetworkPolicy(mode="open", allow_private_network=True)

    def model(self, model_id: str) -> ResolvedModel:
        return ResolvedModel(
            provider="openai-compatible",
            model=model_id,
            endpoint_url=self.endpoint,
            credential_ref=self.credential_ref,
            parameters=ModelParameters(),
            wire_api="chat",
        )

    @staticmethod
    def _error(errors: dict[str, str], case: str, exc: Exception) -> str:
        category, status, _retry_after = classify_gateway_error(exc)
        errors[case] = f"{category}:{status or 'none'}"
        return category

    @staticmethod
    def _capability_failure(category: str) -> bool | None:
        # Only a deterministic provider rejection proves lack of capability.
        # Congestion, disconnects, auth and a stale model id are inconclusive.
        return False if category == "invalid_request" else None

    async def probe(self, model_id: str) -> ProbeResult:
        model = self.model(model_id)
        result = ProbeResult(model_id=model_id, errors={})
        try:
            response = await self.client.complete(
                model,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                network_policy=self.network_policy,
                timeout_seconds=self.timeout_seconds,
                max_attempts=1,
                backoff_seconds=0,
            )
            result.basic_chat = bool(response.content.strip())
            result.available = True
            result.reasoning = True if response.reasoning.strip() else None
            result.usage_reported = bool(response.usage.reported)
        except Exception as exc:  # noqa: BLE001 - report stable classification
            category = self._error(result.errors, "basic_chat", exc)
            result.available = False if category == "not_found" else None
            result.basic_chat = self._capability_failure(category)

        tool_schema = {
            "type": "function",
            "function": {
                "name": "matrix_echo",
                "description": "Return the supplied value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
        try:
            response = await self.client.complete(
                model,
                messages=[
                    {
                        "role": "user",
                        "content": "Call matrix_echo once with value compatibility-probe.",
                    }
                ],
                tools=[tool_schema],
                allow_empty=True,
                network_policy=self.network_policy,
                timeout_seconds=self.timeout_seconds,
                max_attempts=1,
                backoff_seconds=0,
            )
            result.tool_calling = any(call.name == "matrix_echo" for call in response.tool_calls)
        except Exception as exc:  # noqa: BLE001
            category = self._error(result.errors, "tool_calling", exc)
            result.tool_calling = self._capability_failure(category)

        try:
            saw_text = saw_tool = saw_usage = saw_reasoning = False
            async for event in self.client.stream_events(
                model,
                messages=[
                    {
                        "role": "user",
                        "content": "Call matrix_echo once with value stream-probe.",
                    }
                ],
                tools=[tool_schema],
                network_policy=self.network_policy,
                timeout_seconds=self.timeout_seconds,
            ):
                saw_text |= event.type is ModelStreamEventType.TEXT_DELTA
                saw_tool |= event.type is ModelStreamEventType.TOOL_CALL_COMPLETED
                saw_usage |= event.type is ModelStreamEventType.USAGE_UPDATED
                saw_reasoning |= event.type is ModelStreamEventType.REASONING_DELTA
            result.stream_text = saw_text or saw_tool
            result.stream_tool_calling = saw_tool
            result.usage_reported = bool(result.usage_reported or saw_usage)
            result.reasoning = True if result.reasoning or saw_reasoning else None
        except Exception as exc:  # noqa: BLE001
            category = self._error(result.errors, "stream", exc)
            result.stream_text = self._capability_failure(category)
            result.stream_tool_calling = self._capability_failure(category)

        if self.long_context_chars:
            try:
                response = await self.client.complete(
                    model,
                    messages=[
                        {
                            "role": "user",
                            "content": "A" * self.long_context_chars
                            + "\nReply with exactly: LONG_CONTEXT_OK",
                        }
                    ],
                    network_policy=self.network_policy,
                    timeout_seconds=self.timeout_seconds,
                    max_attempts=1,
                    backoff_seconds=0,
                    max_output_tokens=32,
                )
                result.long_context = bool(response.content.strip())
            except Exception as exc:  # noqa: BLE001
                category = self._error(result.errors, "long_context", exc)
                result.long_context = self._capability_failure(category)

        if self.probe_multimodal:
            try:
                response = await self.client.complete(
                    model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Describe the image in one word."},
                                {"type": "image_url", "image_url": {"url": _ONE_PIXEL_PNG}},
                            ],
                        }
                    ],
                    network_policy=self.network_policy,
                    timeout_seconds=self.timeout_seconds,
                    max_attempts=1,
                    backoff_seconds=0,
                    max_output_tokens=32,
                )
                result.multimodal = bool(response.content.strip())
            except Exception as exc:  # noqa: BLE001
                category = self._error(result.errors, "multimodal", exc)
                result.multimodal = self._capability_failure(category)
        if not result.errors:
            result.errors = None
        return result

    async def run(self, model_ids: list[str]) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "gateway_id": self.gateway_id,
            "endpoint": self.endpoint,
            "results": [asdict(await self.probe(model_id)) for model_id in model_ids],
            # 429/disconnect are deterministic transport conformance scenarios,
            # covered by fault-injection tests rather than intentionally
            # attacking a live Gateway endpoint.
            "transportConformance": {
                "rateLimitClassification": "covered-by-fault-injection",
                "disconnectClassification": "covered-by-fault-injection",
            },
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the KsADK model compatibility matrix")
    parser.add_argument("--endpoint", required=True, help="Exact chat-completions endpoint")
    parser.add_argument("--models", required=True, help="Comma-separated Gateway model ids")
    parser.add_argument("--gateway-id", default="ksyun")
    parser.add_argument("--credential-env", default="OPENAI_API_KEY")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--long-context-chars", type=int, default=65536)
    parser.add_argument("--skip-multimodal", action="store_true")
    parser.add_argument("--report-out", required=True)
    parser.add_argument("--declare-out", required=True)
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    credential_env = str(args.credential_env).strip()
    if not os.getenv(credential_env):
        raise SystemExit(f"credential environment variable is not configured: {credential_env}")
    resolver = CredentialResolver()
    client = ModelGatewayClient(credential_resolver=resolver)
    matrix = ModelCompatibilityMatrix(
        client,
        endpoint=args.endpoint,
        credential_ref=f"env://{credential_env}",
        gateway_id=args.gateway_id,
        timeout_seconds=args.timeout,
        long_context_chars=args.long_context_chars,
        probe_multimodal=not args.skip_multimodal,
    )
    try:
        report = await matrix.run(
            [item.strip() for item in str(args.models).split(",") if item.strip()]
        )
    finally:
        await client.aclose()
    Path(args.report_out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    store = ModelCapabilityStore()
    apply_matrix_report(report, store)
    store.save(args.declare_out)
    return 0


def main() -> int:
    return asyncio.run(_main_async(_parser().parse_args()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["ModelCompatibilityMatrix", "ProbeResult", "main"]
