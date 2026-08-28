"""Subprocess fixture for durable high-risk MCP approval recovery E2E."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from ksadk.harness.capabilities import CapabilityDescriptor, RiskLevel
from ksadk.harness.capability_runtime import CapabilityRuntime
from ksadk.harness.engine.mcp_disclosure import (
    MCP_CALL_TOOL_TOOL,
    MCP_LIST_TOOLS_TOOL,
    MCP_READ_SCHEMA_TOOL,
)
from ksadk.harness.mcp_runtime import McpCapabilityRuntime, McpServerBinding, McpTransport
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.runtime_server import build_deployment_app
from ksadk.harness.tool_receipts import ToolReceiptStore

MCP_REF = "mcp://finance-tools@1.0.0"


class _HttpToolTransport(McpTransport):
    def __init__(self, tool_url: str) -> None:
        self._tool_url = tool_url

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "pay_invoice",
                "description": "支付指定发票",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "invoice_id": {"type": "string"},
                        "amount": {"type": "number"},
                    },
                    "required": ["invoice_id", "amount"],
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        body = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")

        def invoke() -> dict[str, Any]:
            request = urllib.request.Request(
                self._tool_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        return await asyncio.to_thread(invoke)


class _CheckpointAwareReasoner:
    """Select the next disclosure step only from durable transcript messages."""

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
    ) -> HarnessReasoningTurn:
        del model, prompt, tools
        completed_tools = {
            str(message.get("name") or "")
            for message in messages
            if message.get("role") == "tool"
        }
        if MCP_LIST_TOOLS_TOOL not in completed_tools:
            return _call("list-tools", MCP_LIST_TOOLS_TOOL, {"server_id": MCP_REF})
        if MCP_READ_SCHEMA_TOOL not in completed_tools:
            return _call(
                "read-schema",
                MCP_READ_SCHEMA_TOOL,
                {"server_id": MCP_REF, "tool_name": "pay_invoice"},
            )
        if MCP_CALL_TOOL_TOOL not in completed_tools:
            return _call(
                "pay-invoice",
                MCP_CALL_TOOL_TOOL,
                {
                    "server_id": MCP_REF,
                    "tool_name": "pay_invoice",
                    "arguments": {"invoice_id": "INV-E2E-1", "amount": 88},
                },
            )
        return HarnessReasoningTurn(final_text="invoice-paid-once")


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> HarnessReasoningTurn:
    return HarnessReasoningTurn(
        tool_calls=(HarnessToolCall(call_id=call_id, name=name, arguments=arguments),)
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--build-id", default="")
    parser.add_argument("--content-hash", default="")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--tool-url", required=True)
    parser.add_argument("--crash-after-receipt", default="")
    return parser.parse_args()


class _CrashAfterReceiptRuntime(CapabilityRuntime):
    """Exit only after the executed Receipt is durably committed once."""

    def __init__(self, *, receipts: ToolReceiptStore, marker: str) -> None:
        super().__init__(receipts=receipts)
        self._marker = Path(marker)

    def record_receipt(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        prior = super().record_receipt(**kwargs)
        if (
            kwargs.get("status") == "executed"
            and kwargs.get("tool_name") == MCP_CALL_TOOL_TOOL
            and not self._marker.exists()
        ):
            self._marker.write_text("receipt-committed", encoding="utf-8")
            os._exit(91)
        return prior


def main() -> None:
    args = _parse_args()
    runtime = McpCapabilityRuntime()
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=MCP_REF,
                kind="mcp",
                name="财务支付服务",
                description="用于高风险支付审批恢复测试",
                version="1.0.0",
                risk_level=RiskLevel.HIGH,
            ),
            transport=_HttpToolTransport(args.tool_url),
            required=True,
        )
    )
    spec_payload = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    engine_kwargs: dict[str, Any] = {"mcp_runtime": runtime}
    if args.crash_after_receipt:
        state_dir = Path(args.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        receipts = ToolReceiptStore(str(state_dir / "tool_receipts.sqlite"))
        engine_kwargs["capability_runtime"] = _CrashAfterReceiptRuntime(
            receipts=receipts,
            marker=args.crash_after_receipt,
        )
    app = build_deployment_app(
        deployment_id=args.deployment_id,
        route=args.route,
        spec_payload=spec_payload,
        build_id=args.build_id,
        content_hash=args.content_hash,
        reasoner=_CheckpointAwareReasoner(),
        engine_kwargs=engine_kwargs,
        state_dir=args.state_dir,
    )
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
