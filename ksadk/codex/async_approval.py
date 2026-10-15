"""Route Codex server requests through approval and user-input handlers."""

from __future__ import annotations

import queue
from typing import Any
from uuid import uuid4

from ksadk.codex.client import _PendingApproval, _PendingInteraction


class _AsyncApprovalMixin:
    def _install_approval_bridge(self) -> None:
        """Replace the SDK's unconditional accept handler with a HITL bridge.

        ``openai-codex==0.147.0`` exposes approval callbacks only on its sync
        JSON-RPC client.  The public ``AsyncCodex`` wrapper owns that client, so
        this pinned compatibility seam is validated eagerly instead of silently
        auto-accepting tool and file changes.
        """

        async_client = getattr(self._codex, "_client", None)
        sync_client = getattr(async_client, "_sync", None)
        if sync_client is None or not hasattr(sync_client, "_approval_handler"):
            raise RuntimeError(
                f"openai-codex {self.sdk_version} 不支持交互式审批桥接(内部 API 不兼容)"
            )
        sync_client._approval_handler = self._handle_server_request

    def _active_request_queue(self, raw: dict[str, Any]) -> tuple[str, queue.Queue[Any] | None]:
        thread_id = str(raw.get("threadId") or raw.get("thread_id") or "")
        if not thread_id:
            with self._approval_lock:
                candidates = list(self._approval_queues)
            if len(candidates) == 1:
                thread_id = candidates[0]
        with self._approval_lock:
            return thread_id, self._approval_queues.get(thread_id)

    def _handle_server_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if method == "item/tool/call":
            from ksadk.codex.a2a_tools import failed_tool_result

            bridge = getattr(self, "_a2a_expert_bridge", None)
            return (
                bridge.request(params)
                if bridge is not None
                else failed_tool_result("A2A_CODEX_TOOL_NOT_REGISTERED")
            )
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return self._handle_approval_request(method, params)
        if method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
            return self._handle_user_input_request(method, params)
        return {}

    def _handle_approval_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raw = dict(params or {})
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {}
        thread_id, approval_queue = self._active_request_queue(raw)
        approval_id = str(
            raw.get("itemId") or raw.get("item_id") or raw.get("id") or f"approval_{uuid4().hex}"
        )
        pending = _PendingApproval(
            approval_id=approval_id,
            thread_id=thread_id,
            method=method,
            params=raw,
        )
        with self._approval_lock:
            if approval_queue is None:
                # Fail closed when a request cannot be associated with an
                # active run; never fall back to the SDK's auto-accept default.
                return {"decision": "decline"}
            self._pending_approvals[approval_id] = pending
            # 下发原生 JSON-RPC requestApproval 消息（synthetic id=approval_id）：
            # canonical mapper 只认原生方法（unsupported_method fail-closed），
            # 旧合成 ``item/approval/requested`` 事件会让整个 run 失败。
            approval_queue.put(
                {
                    "id": approval_id,
                    "method": method,
                    "params": raw,
                }
            )
        pending.resolved.wait()
        with self._approval_lock:
            self._pending_approvals.pop(approval_id, None)
        # 回包也以 JSON-RPC response 形态下发，mapper 才会产出
        # InteractionResolved（原 call_id 闭环）并释放 continuation。
        response = pending.response or {"decision": "decline"}
        if approval_queue is not None:
            approval_queue.put(
                {
                    "id": approval_id,
                    "result": dict(response),
                }
            )
        return pending.response or {"decision": "decline"}

    def _handle_user_input_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raw = dict(params or {})
        thread_id, request_queue = self._active_request_queue(raw)
        metadata = raw.get("_meta") or {}
        # Full access authorizes tool execution, never OAuth login or arbitrary
        # server forms. Only Codex's native, empty tool-approval form qualifies.
        schema = raw.get("requestedSchema") or {}
        # MCP 工具调用不属于风险操作：无论沙箱/审批档位如何，codex 的
        # mcp_tool_call 审批 elicitation（原生空表单）一律自动放行；真正的
        # 风险操作（shell 命令/文件改动审批、真实表单 elicitation）不受影响。
        if (
            method == "mcpServer/elicitation/request"
            and request_queue is not None
            and isinstance(metadata, dict)
            and metadata.get("codex_approval_kind") == "mcp_tool_call"
            and raw.get("mode") == "form"
            and schema == {"type": "object", "properties": {}}
        ):
            return {"action": "accept", "content": {}}
        interaction_id = str(
            raw.get("itemId")
            or raw.get("item_id")
            or raw.get("requestId")
            or raw.get("request_id")
            or raw.get("id")
            or f"input_{uuid4().hex}"
        )
        pending = _PendingInteraction(
            interaction_id=interaction_id,
            thread_id=thread_id,
            method=method,
            params=raw,
        )
        default_response = (
            {"action": "cancel"} if method == "mcpServer/elicitation/request" else {"answers": {}}
        )
        if request_queue is None:
            return default_response

        # Preserve the native question contract (including multiple choice and
        # custom answers) for the canonical interaction mapper. Legacy A2UI
        # forms cannot provide the native continuation/response correlation.
        with self._approval_lock:
            self._pending_interactions[interaction_id] = pending
            request_queue.put({"id": interaction_id, "method": method, "params": raw})
        pending.resolved.wait()
        with self._approval_lock:
            self._pending_interactions.pop(interaction_id, None)
        response = pending.response or default_response
        request_queue.put({"id": interaction_id, "result": dict(response)})
        return response
