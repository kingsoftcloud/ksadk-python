"""Async Codex backend implementation.

The protocol and marketplace contracts stay in :mod:`ksadk.codex.client`;
this module owns the provider lifecycle, approval bridge, and turn streams.
"""

from __future__ import annotations

import asyncio
import os
import queue
import secrets
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from ksadk.codex import client as _client_module
from ksadk.codex.client import (
    CodexClient,
    CodexPluginBootstrap,
    CodexPluginBootstrapError,
    _CapabilityRoute,
    _is_openai_official,
    _missing_codex_error,
    _PendingApproval,
    _PendingInteraction,
    _upgrade_http_to_https,
)
from ksadk.codex.client_payloads import (
    coerce_input,
    notification_to_event_dict,
    thread_kwargs,
    turn_kwargs,
)
from ksadk.model_proxy import ProxyConfig, ProxyServer

from ksadk.codex.async_approval import _AsyncApprovalMixin

from ksadk.codex.async_proxy import _AsyncProxyMixin


class AsyncCodexClient(_AsyncProxyMixin, _AsyncApprovalMixin, CodexClient):
    """基于 OpenAI 官方 ``openai-codex`` SDK(``AsyncCodex``)的生产后端。

    **lazy import** ``openai_codex``:仅在实际使用时导入,缺依赖时显式报错
    (``pip install 'ksadk[codex]'``),不让所有 ksadk 用户默认背几十 MB CLI 二进制。
    构造时校验真实 SDK 方法存在(``thread_start``/``thread_resume``/``close`` 及
    ``AsyncThread.turn`` / ``AsyncTurnHandle.stream``/``interrupt``),版本漂移 fail-fast。
    """

    _notification_to_event_dict = staticmethod(notification_to_event_dict)

    @staticmethod
    def _start_proxy_and_inject(config: Any, *, proxy_observer: Any = None) -> tuple[Any, Any]:
        return _AsyncProxyMixin._start_proxy_and_inject(
            config, proxy_observer=proxy_observer,
            proxy_factory=ProxyServer, config_factory=ProxyConfig,
        )

    def __init__(self, config: Any = None, *, proxy_observer: Any = None) -> None:
        try:
            from openai_codex import (  # type: ignore[import-not-found]
                AsyncCodex,
                AsyncThread,
                AsyncTurnHandle,
            )
        except ImportError as exc:
            raise _missing_codex_error() from exc

        try:
            sdk_version = version("openai-codex")
        except PackageNotFoundError:
            sdk_version = "unknown"
        required = (
            (AsyncCodex, "thread_start"),
            (AsyncCodex, "thread_resume"),
            (AsyncCodex, "close"),
            (AsyncThread, "turn"),
            (AsyncTurnHandle, "stream"),
            (AsyncTurnHandle, "interrupt"),
        )
        for owner, method_name in required:
            if not hasattr(owner, method_name):
                raise RuntimeError(
                    f"openai-codex {sdk_version} SDK 缺少 "
                    f"{owner.__name__}.{method_name}(版本不兼容)"
                )

        # AsyncCodex 0.147.0 only accepts one CodexConfig positional/keyword.
        config, self._proxy = self._maybe_apply_proxy(
            config,
            proxy_observer=proxy_observer,
        )
        self._codex = AsyncCodex(config=config)
        self._checkpoint_home = (getattr(config, "env", None) or {}).get("CODEX_HOME")
        self.sdk_version = sdk_version
        self._threads: dict[str, Any] = {}  # thread_id -> AsyncThread
        self._thread_approval_configs: dict[str, dict[str, Any]] = {}
        self._active_handles: dict[str, Any] = {}  # thread_id -> 活跃 AsyncTurnHandle
        self._goal_states: dict[str, Any] = {}
        self._approval_queues: dict[str, queue.Queue[Any]] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._pending_interactions: dict[str, _PendingInteraction] = {}
        self._approval_lock = threading.Lock()
        self._a2a_expert_bridge = None
        self._install_approval_bridge()

    async def bootstrap_plugins(self, config: CodexPluginBootstrap) -> None:
        """Install a pinned local marketplace on this client's App Server.

        This intentionally uses the ``AsyncCodex`` object's already-owned
        low-level transport. Spawning a second bridge process would install
        into a different host lifecycle and would not guarantee that the
        thread started below observes the same plugin inventory.
        """

        config.verify()
        try:
            from openai_codex.generated.v2_all import (  # type: ignore[import-not-found]
                MarketplaceAddResponse,
                PluginInstallResponse,
                PluginListResponse,
            )
        except ImportError as exc:  # pragma: no cover - guarded by __init__
            raise _missing_codex_error() from exc

        await self._codex._ensure_initialized()
        transport = getattr(self._codex, "_client", None)
        if transport is None or not callable(getattr(transport, "request", None)):
            raise CodexPluginBootstrapError(
                f"openai-codex {self.sdk_version} lacks the App Server request transport"
            )

        receipt = await transport.request(
            "marketplace/add",
            {
                "source": str(config.root),
                "refName": None,
                "sparsePaths": None,
            },
            response_model=MarketplaceAddResponse,
        )
        if str(receipt.marketplace_name) != config.marketplace_name:
            raise CodexPluginBootstrapError(
                "Codex App Server added an unexpected marketplace: "
                f"expected {config.marketplace_name!r}, got {receipt.marketplace_name!r}"
            )
        installed_root_value = getattr(receipt.installed_root, "root", receipt.installed_root)
        try:
            installed_root = Path(str(installed_root_value)).resolve(strict=True)
            expected_root = config.root.resolve(strict=True)
        except OSError as exc:
            raise CodexPluginBootstrapError(
                "Codex App Server marketplace receipt names an unavailable root"
            ) from exc
        if installed_root != expected_root:
            raise CodexPluginBootstrapError(
                "Codex App Server marketplace receipt does not match the pinned root"
            )

        marketplace_manifest = str(config.manifest_path.resolve(strict=True))
        for plugin_name in config.plugin_names:
            await transport.request(
                "plugin/install",
                {
                    "pluginName": plugin_name,
                    "marketplacePath": marketplace_manifest,
                    "remoteMarketplaceName": None,
                },
                response_model=PluginInstallResponse,
            )

        inventory = await transport.request(
            "plugin/list",
            {"forceRefetch": True, "cwds": None, "marketplaceKinds": None},
            response_model=PluginListResponse,
        )
        marketplaces = [
            marketplace
            for marketplace in inventory.marketplaces
            if str(marketplace.name) == config.marketplace_name
        ]
        if len(marketplaces) != 1:
            raise CodexPluginBootstrapError(
                "Codex App Server did not report exactly one pinned marketplace"
            )
        marketplace = marketplaces[0]
        reported_path_value = getattr(marketplace.path, "root", marketplace.path)
        if reported_path_value is None:
            raise CodexPluginBootstrapError(
                "Codex App Server reported the pinned marketplace as remote"
            )
        try:
            reported_path = Path(str(reported_path_value)).resolve(strict=True)
        except OSError as exc:
            raise CodexPluginBootstrapError(
                "Codex App Server reported an unavailable marketplace manifest"
            ) from exc
        if reported_path != Path(marketplace_manifest):
            raise CodexPluginBootstrapError(
                "Codex App Server inventory does not match the pinned marketplace path"
            )
        for plugin_name in config.plugin_names:
            matches = [plugin for plugin in marketplace.plugins if plugin.name == plugin_name]
            if len(matches) != 1 or not matches[0].installed or not matches[0].enabled:
                raise CodexPluginBootstrapError(
                    "Codex App Server did not reconcile pinned plugin as installed and enabled: "
                    + plugin_name
                )

    async def start_thread(self, config: Optional[dict[str, Any]] = None) -> str:
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if bridge is not None and bridge.experts:
            thread = await self._start_expert_thread(config)
            bridge.register_thread(str(thread.id))
        elif self._uses_manual_approval(config):
            thread = await self._start_manual_thread(config)
        else:
            thread = await self._codex.thread_start(**thread_kwargs(config))
        self._threads[thread.id] = thread
        return str(thread.id)

    async def compact_thread(self, thread_id: str) -> dict[str, Any]:
        """Wait for native compaction completion, not just request admission.

        The caller owns an idle, dedicated client and closes it on timeout.
        No model prompt or shell command is used to emulate compaction.
        """
        thread = self._threads[thread_id]
        low_level = self._codex._client
        turn_id = None
        compacted = False
        usage: dict[str, Any] = {}
        try:
            async with asyncio.timeout(120):
                before = await thread.read(include_turns=True)
                previous_ids = {turn.id for turn in before.thread.turns}
                await thread.compact()
                # compact/start returns no turn id. The SDK buffers scoped
                # notifications until their turn is registered; global reads
                # deliberately exclude them. Discover the new turn first.
                while turn_id is None:
                    current = await thread.read(include_turns=True)
                    new_turns = [
                        turn for turn in current.thread.turns if turn.id not in previous_ids
                    ]
                    if new_turns:
                        turn_id = new_turns[-1].id
                        low_level.register_turn_notifications(turn_id)
                        break
                    await asyncio.sleep(0.25)
                while True:
                    notification = await low_level.next_turn_notification(turn_id)
                    event = self._notification_to_event_dict(notification) or {}
                    params = event.get("params") or {}
                    if params.get("threadId") != thread_id:
                        continue
                    method = event.get("method")
                    if method == "item/completed":
                        compacted |= (params.get("item") or {}).get("type") == "contextCompaction"
                    elif method == "thread/tokenUsage/updated":
                        usage = dict(params.get("tokenUsage") or {})
                    elif method == "turn/completed":
                        turn = params["turn"]
                        if turn.get("status") != "completed" or not compacted:
                            raise RuntimeError("Codex context compaction did not complete")
                        return usage
        finally:
            if turn_id is not None:
                low_level.unregister_turn_notifications(turn_id)

    async def resume_thread(self, thread_id: str, config: Optional[dict[str, Any]] = None) -> str:
        # An ephemeral thread has no rollout on disk, so SDK thread_resume would
        # fail with -32600. Reuse its live AsyncThread for same-process resume.
        cached = self._threads.get(thread_id)
        if cached is not None:
            return str(cached.id)
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if bridge is not None and bridge.experts:
            raise ValueError("A2A_CODEX_EXPERT_RESUME_UNVERIFIED")
        if self._uses_manual_approval(config):
            thread = await self._resume_manual_thread(thread_id, config)
        else:
            kwargs = thread_kwargs(config)
            # AsyncCodex.thread_resume 0.144.4 has no ephemeral parameter.
            kwargs.pop("ephemeral", None)
            thread = await self._codex.thread_resume(thread_id, **kwargs)
        self._threads[thread.id] = thread
        return str(thread.id)

    @staticmethod
    def _uses_manual_approval(config: Optional[dict[str, Any]]) -> bool:
        return str((config or {}).get("approval_mode") or "").strip().lower() == "manual"

    async def _start_manual_thread(self, config: Optional[dict[str, Any]]) -> Any:
        """Start a thread whose native approvals are reviewed by Studio users.

        ``openai-codex==0.147.0`` exposes ``ApprovalsReviewer.user`` on the
        generated app-server contract but omits it from the public
        ``ApprovalMode`` enum. Use that pinned wire contract explicitly rather
        than falling back to ``auto_review``.
        """

        from openai_codex import AsyncThread  # type: ignore[import-not-found]
        from openai_codex._sandbox import _sandbox_mode  # type: ignore[import-not-found]
        from openai_codex.generated.v2_all import (  # type: ignore[import-not-found]
            ApprovalsReviewer,
            AskForApproval,
            AskForApprovalValue,
            ThreadStartParams,
        )

        kwargs = thread_kwargs(self._without_manual_approval(config))
        kwargs.pop("approval_mode", None)
        sandbox = kwargs.pop("sandbox", None)
        await self._codex._ensure_initialized()
        started = await self._codex._client.thread_start(
            ThreadStartParams(
                approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
                approvals_reviewer=ApprovalsReviewer.user,
                sandbox=_sandbox_mode(sandbox),
                **kwargs,
            )
        )
        return AsyncThread(self._codex, started.thread.id)

    async def _resume_manual_thread(
        self,
        thread_id: str,
        config: Optional[dict[str, Any]],
    ) -> Any:
        from openai_codex import AsyncThread  # type: ignore[import-not-found]
        from openai_codex._sandbox import _sandbox_mode  # type: ignore[import-not-found]
        from openai_codex.generated.v2_all import (  # type: ignore[import-not-found]
            ApprovalsReviewer,
            AskForApproval,
            AskForApprovalValue,
            ThreadResumeParams,
        )

        kwargs = thread_kwargs(self._without_manual_approval(config))
        kwargs.pop("approval_mode", None)
        kwargs.pop("ephemeral", None)
        sandbox = kwargs.pop("sandbox", None)
        await self._codex._ensure_initialized()
        resumed = await self._codex._client.thread_resume(
            thread_id,
            ThreadResumeParams(
                thread_id=thread_id,
                approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
                approvals_reviewer=ApprovalsReviewer.user,
                sandbox=_sandbox_mode(sandbox),
                **kwargs,
            ),
        )
        return AsyncThread(self._codex, resumed.thread.id)

    @staticmethod
    def _without_manual_approval(
        config: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        result = dict(config or {})
        result.pop("approval_mode", None)
        return result

    def run_turn(
        self,
        thread_id: str,
        prompt: Any,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._run_turn_gen(thread_id, prompt, config)

    def run_goal(
        self,
        thread_id: str,
        objective: str,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._run_goal_gen(thread_id, objective, config)

    async def _run_turn_gen(
        self, thread_id: str, prompt: Any, config: Optional[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        thread = self._threads.get(thread_id)
        if thread is None:
            # 未显式 start/resume 的 thread_id:按 resume 语义接入真实后端。
            await self.resume_thread(thread_id, config)
            thread = self._threads[thread_id]
        approval_queue: queue.Queue[Any] = queue.Queue()
        with self._approval_lock:
            self._approval_queues[thread_id] = approval_queue
            self._thread_approval_configs[thread_id] = dict(config or {})
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if bridge is not None and bridge.experts:
            bridge.activate(thread_id, approval_queue)
        try:
            handle = await self._start_turn(thread, coerce_input(prompt), config)
        except BaseException:
            if bridge is not None:
                bridge.deactivate(thread_id)
            with self._approval_lock:
                self._approval_queues.pop(thread_id, None)
            raise
        self._active_handles[thread_id] = handle
        notifications = handle.stream()
        notification_task: Any = None
        approval_task: Any = None
        try:
            while True:
                if notification_task is None:
                    notification_task = asyncio.create_task(notifications.__anext__())
                if approval_task is None:
                    approval_task = asyncio.create_task(asyncio.to_thread(approval_queue.get))
                done, _pending = await asyncio.wait(
                    {notification_task, approval_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if approval_task in done:
                    approval_event = approval_task.result()
                    approval_task = None
                    if approval_event is not None:
                        yield approval_event
                if notification_task in done:
                    try:
                        notification = notification_task.result()
                    except StopAsyncIteration:
                        break
                    notification_task = None
                    event = self._notification_to_event_dict(notification)
                    if event is not None:
                        yield event
        finally:
            if bridge is not None:
                bridge.deactivate(thread_id)
            approval_queue.put(None)
            for task in (notification_task, approval_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (notification_task, approval_task) if task is not None),
                return_exceptions=True,
            )
            self._active_handles.pop(thread_id, None)
            with self._approval_lock:
                self._approval_queues.pop(thread_id, None)

    async def _run_goal_gen(
        self,
        thread_id: str,
        objective: str,
        config: Optional[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if bridge is not None and bridge.experts:
            raise ValueError("A2A_CODEX_EXPERT_GOAL_UNSUPPORTED")
        if thread_id not in self._threads:
            await self.resume_thread(thread_id, config)
        from openai_codex._goal import _AsyncGoalNotificationStream

        low_level = self._codex._client
        approval_queue: queue.Queue[Any] = queue.Queue()
        with self._approval_lock:
            self._approval_queues[thread_id] = approval_queue
            self._thread_approval_configs[thread_id] = dict(config or {})
        state, _logical_turn_id = await low_level.start_goal_operation(thread_id, objective)
        self._goal_states[thread_id] = state
        notifications = _AsyncGoalNotificationStream(
            state=state,
            next_notification=lambda: low_level.next_goal_notification(state),
            unregister=lambda: low_level.unregister_goal_operation(state),
            cancel_goal=lambda: low_level.cancel_goal_operation(state),
        )
        notification_task: Any = None
        approval_task: Any = None
        try:
            while True:
                if notification_task is None:
                    notification_task = asyncio.create_task(notifications.__anext__())
                if approval_task is None:
                    approval_task = asyncio.create_task(asyncio.to_thread(approval_queue.get))
                done, _pending = await asyncio.wait(
                    {notification_task, approval_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if approval_task in done:
                    approval_event = approval_task.result()
                    approval_task = None
                    if approval_event is not None:
                        yield approval_event
                if notification_task in done:
                    try:
                        notification = notification_task.result()
                    except StopAsyncIteration:
                        break
                    notification_task = None
                    event = self._notification_to_event_dict(notification)
                    if event is not None:
                        yield event
        finally:
            approval_queue.put(None)
            for task in (notification_task, approval_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (notification_task, approval_task) if task is not None),
                return_exceptions=True,
            )
            await notifications.aclose()
            self._goal_states.pop(thread_id, None)
            with self._approval_lock:
                self._approval_queues.pop(thread_id, None)

    async def _start_turn(
        self,
        thread: Any,
        prompt: Any,
        config: Optional[dict[str, Any]],
    ) -> Any:
        mode = str((config or {}).get("collaboration_mode") or "").strip().lower()
        if not mode or mode == "default":
            return await thread.turn(prompt, **turn_kwargs(config))
        if mode != "plan":
            raise ValueError("collaboration_mode must be default or plan")

        # openai-codex 0.144.4 predates the public collaboration_mode argument,
        # while the bundled app-server already accepts it. Keep this private
        # compatibility seam small and covered by a wire-payload test.
        from openai_codex import AsyncTurnHandle
        from openai_codex._approval_mode import _approval_mode_override_settings
        from openai_codex._inputs import _normalize_run_input, _to_wire_input
        from openai_codex._sandbox import _sandbox_policy
        from openai_codex.generated.v2_all import TurnStartParams

        turn_config = turn_kwargs(config)
        approval_mode = turn_config.pop("approval_mode", None)
        sandbox = turn_config.pop("sandbox", None)
        approval_policy, approvals_reviewer = _approval_mode_override_settings(approval_mode)
        wire_input = _to_wire_input(_normalize_run_input(prompt))
        params = TurnStartParams(
            thread_id=thread.id,
            input=wire_input,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox_policy=_sandbox_policy(sandbox),
            **turn_config,
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        params["collaborationMode"] = {
            "mode": "plan",
            "settings": {
                "model": str((config or {}).get("model") or ""),
                "reasoning_effort": None,
                "developer_instructions": None,
            },
        }
        started = await self._codex._client.turn_start(thread.id, wire_input, params=params)
        return AsyncTurnHandle(self._codex, thread.id, started.turn.id)

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        handle = self._active_handles.get(thread_id)
        if handle is None:
            return False
        # The SDK reader synchronously waits for dynamic tool responses.
        # Release callbacks before waiting for the interrupt RPC response.
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if bridge is not None:
            bridge.deactivate(thread_id)
        await handle.interrupt()
        return True

    async def pause_goal(self, thread_id: str) -> bool:
        state = self._goal_states.get(thread_id)
        if state is None:
            return False
        await self._codex._client.pause_goal(thread_id)
        # Pausing the persisted goal only prevents its next continuation.
        # The SDK cleanup also interrupts the active physical turn (including
        # a racing continuation), while retaining the paused goal and thread.
        # Without it the runtime drain deadline closes the model proxy, making
        # the next resume reconnect to a dead local address.
        await self._codex._client.cancel_goal_operation(state)
        return True

    async def cancel_goal(self, thread_id: str) -> bool:
        state = self._goal_states.get(thread_id)
        if state is None:
            return False
        await self._codex._client.cancel_goal_operation(state)
        return True

    async def resolve_approval(self, approval_id: str, decision: str) -> bool:
        normalized = {
            "approve": "accept",
            "accept": "accept",
            "approve_session": "acceptForSession",
            "accept_for_session": "acceptForSession",
            "deny": "decline",
            "decline": "decline",
            "cancel": "cancel",
        }.get(str(decision).strip().lower())
        if normalized is None:
            raise ValueError("approval decision must be approve, approve_session, deny, or cancel")
        with self._approval_lock:
            pending = self._pending_approvals.get(approval_id)
            if pending is None:
                return False
            pending.response = {"decision": normalized}
            pending.resolved.set()
        return True

    async def resolve_interaction(
        self,
        interaction_id: str,
        data: dict[str, Any],
    ) -> bool:
        answers: dict[str, dict[str, list[str]]] = {}
        for key, value in data.items():
            if key in {"decision", "name", "action"} or value is None:
                continue
            raw_answers = value if isinstance(value, list) else [value]
            normalized = [str(answer) for answer in raw_answers if str(answer)]
            answers[str(key)] = {"answers": normalized}
        with self._approval_lock:
            pending = self._pending_interactions.get(interaction_id)
            if pending is None:
                return False
            if pending.method == "mcpServer/elicitation/request":
                action = data.get("action") or {
                    "cancel": "cancel",
                    "skip": "decline",
                    "reject": "decline",
                    "deny": "decline",
                    "decline": "decline",
                }.get(str(data.get("decision") or ""), "accept")
                if action not in {"accept", "decline", "cancel"}:
                    raise ValueError("MCP elicitation action must be accept, decline, or cancel")
                pending.response = {"action": action}
                if action == "accept":
                    pending.response["content"] = {
                        key: value
                        for key, value in data.items()
                        if key not in {"action", "decision"}
                    }
            else:
                pending.response = {"answers": answers}
            pending.resolved.set()
        return True

    async def close(self) -> None:
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if bridge is not None:
            bridge.close()
        approval_lock = getattr(self, "_approval_lock", None)
        if approval_lock is None:
            pending: list[_PendingApproval] = []
        else:
            with approval_lock:
                pending = list(getattr(self, "_pending_approvals", {}).values())
        for approval in pending:
            approval.response = {"decision": "cancel"}
            approval.resolved.set()
        if approval_lock is None:
            interactions: list[_PendingInteraction] = []
        else:
            with approval_lock:
                interactions = list(getattr(self, "_pending_interactions", {}).values())
        for interaction in interactions:
            interaction.response = (
                {"action": "cancel"}
                if interaction.method == "mcpServer/elicitation/request"
                else {"answers": {}}
            )
            interaction.resolved.set()
        for thread_id, state in list(getattr(self, "_goal_states", {}).items()):
            try:
                await self._codex._client.cancel_goal_operation(state)
            except Exception:
                pass
            finally:
                self._goal_states.pop(thread_id, None)
        try:
            await self._codex.close()
        finally:
            self._active_handles.clear()
            self._threads.clear()
            if self._proxy is not None:
                self._proxy.stop()
                self._proxy = None

    def configure_a2a_experts(self, specs):
        from ksadk.codex.a2a_tools import CodexExpertBridge
        if self._threads:
            raise ValueError("A2A_CODEX_EXPERTS_ALREADY_REGISTERED")
        bridge = CodexExpertBridge(specs)
        if bridge.experts:
            from ksadk.codex._expert_request_reader import install_expert_request_reader
            install_expert_request_reader(self._codex._client._sync)
        self._a2a_expert_bridge = bridge


    def resolve_a2a_tool(self, call, response):
        bridge = getattr(self, "_a2a_expert_bridge", None)
        return bridge.resolve(call, response) if bridge is not None else False


    async def capture_a2a_rollout(self, call):
        """Read and validate the real native record before an expert side effect."""
        from pathlib import Path

        from ksadk.a2a.codex import CodexToolCall
        from ksadk.codex.rollout_checkpoint import capture_rollout

        call = CodexToolCall.model_validate(call)
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if not self._checkpoint_home or bridge is None or call.tool not in bridge.experts:
            raise ValueError("A2A_CODEX_CHECKPOINT_CONTEXT_REQUIRED")
        thread = self._threads.get(call.thread_id)
        if thread is None:
            raise ValueError("A2A_CODEX_CHECKPOINT_THREAD_UNKNOWN")
        async with asyncio.timeout(5):
            while True:
                native = await thread.read(include_turns=True)
                if native.thread.id != call.thread_id or not native.thread.path:
                    raise ValueError("A2A_CODEX_CHECKPOINT_NATIVE_IDENTITY_INVALID")
                try:
                    checkpoint = await asyncio.to_thread(
                        capture_rollout,
                        Path(self._checkpoint_home),
                        Path(native.thread.path),
                        thread_id=call.thread_id,
                    )
                    checkpoint.require_call(
                        turn_id=call.turn_id,
                        call_id=call.call_id,
                        tool=call.tool,
                        arguments=call.arguments,
                    )
                    return checkpoint
                except ValueError as exc:
                    # The native writer may not have flushed the call yet.
                    # Identity conflicts, corruption and duplicates never retry.
                    if str(exc) != "CODEX_ROLLOUT_CALL_MISSING":
                        raise
                await asyncio.sleep(0.05)


    async def capture_completed_a2a_rollout(self, thread_id: str, turn_id: str):
        """Capture terminal history for a later turn, including turns without tools."""
        from pathlib import Path

        from ksadk.codex.rollout_checkpoint import capture_rollout

        bridge = getattr(self, "_a2a_expert_bridge", None)
        if not self._checkpoint_home or bridge is None or not bridge.experts:
            raise ValueError("A2A_CODEX_CHECKPOINT_CONTEXT_REQUIRED")
        thread = self._threads.get(thread_id)
        if thread is None:
            raise ValueError("A2A_CODEX_CHECKPOINT_THREAD_UNKNOWN")
        async with asyncio.timeout(5):
            while True:
                native = await thread.read(include_turns=True)
                if native.thread.id != thread_id or not native.thread.path:
                    raise ValueError("A2A_CODEX_CHECKPOINT_NATIVE_IDENTITY_INVALID")
                turns = native.thread.turns
                if not turns or turns[-1].id != turn_id:
                    raise ValueError("A2A_CODEX_CHECKPOINT_NATIVE_TURN_CONFLICT")
                if turns[-1].status.value != "completed":
                    raise ValueError("A2A_CODEX_CHECKPOINT_NATIVE_TURN_NOT_COMPLETED")
                checkpoint = await asyncio.to_thread(
                    capture_rollout, Path(self._checkpoint_home), Path(native.thread.path),
                    thread_id=thread_id,
                )
                try:
                    checkpoint.require_completed_turn(turn_id)
                    return checkpoint
                except ValueError as exc:
                    if str(exc) != "CODEX_ROLLOUT_COMPLETION_MISSING":
                        raise
                await asyncio.sleep(0.05)

    async def ensure_managed_a2a_output(self, call, output: str):
        """Inject one verified expert output and return its flushed native record.

        An RPC timeout is an uncertain write: never resend in this invocation.
        A subsequent recovery must reread the actual native transcript first.
        """
        from pydantic import BaseModel

        from ksadk.a2a.codex import CodexToolCall

        call = CodexToolCall.model_validate(call)
        if not isinstance(output, str):
            raise ValueError("A2A_CODEX_RECOVERY_OUTPUT_INVALID")
        rollout = await self.capture_a2a_rollout(call)
        existing = rollout.function_output(call.call_id)
        if existing is not None:
            if existing != output:
                raise ValueError("A2A_CODEX_RECOVERY_OUTPUT_CONFLICT")
            return rollout

        class EmptyResponse(BaseModel):
            pass

        async with asyncio.timeout(10):
            await self._codex._client.request(
                "thread/inject_items",
                {"threadId": call.thread_id, "items": [
                    {"type": "function_call_output", "call_id": call.call_id, "output": output},
                ]},
                response_model=EmptyResponse,
            )
            while True:
                rollout = await self.capture_a2a_rollout(call)
                existing = rollout.function_output(call.call_id)
                if existing is not None:
                    if existing != output:
                        raise ValueError("A2A_CODEX_RECOVERY_OUTPUT_CONFLICT")
                    return rollout
                await asyncio.sleep(0.05)


    async def restore_completed_a2a_thread(self, checkpoint):
        """Attach a Kernel-validated completed session without replaying its tools."""
        from pathlib import Path

        from ksadk.a2a.codex_session_checkpoint import CodexSessionCheckpoint
        from ksadk.codex.rollout_checkpoint import restore_rollout

        checkpoint = CodexSessionCheckpoint.model_validate(checkpoint.model_dump(by_alias=True))
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if (not self._checkpoint_home or bridge is None
                or checkpoint.bindings != {name: expert.spec for name, expert in bridge.experts.items()}):
            raise ValueError("A2A_CODEX_SESSION_BINDING_CONFLICT")
        # Kernel can consume the terminal event just before the native generator
        # finishes its finally block. Wait for that owner to release the handle;
        # never pop it or interrupt an execution to force a handoff.
        try:
            async with asyncio.timeout(5):
                while checkpoint.thread_id in self._active_handles:
                    await asyncio.sleep(0.01)
        except TimeoutError:
            raise ValueError("A2A_CODEX_SESSION_THREAD_ALREADY_ATTACHED") from None
        native = self._threads.get(checkpoint.thread_id)
        if native is None:
            # Exact-byte restore rejects a newer local turn; never trim or overwrite it.
            await asyncio.to_thread(restore_rollout, Path(self._checkpoint_home), checkpoint.rollout)
        config = dict(checkpoint.start_request.config)
        if checkpoint.start_request.model:
            config["model"] = checkpoint.start_request.model
        async with asyncio.timeout(10):
            if native is None:
                if self._uses_manual_approval(config):
                    native = await self._resume_manual_thread(checkpoint.thread_id, config)
                else:
                    kwargs = thread_kwargs(config)
                    kwargs.pop("ephemeral", None)
                    native = await self._codex.thread_resume(checkpoint.thread_id, **kwargs)
            read = await native.read(include_turns=True)
        if (native.id != checkpoint.thread_id or read.thread.id != checkpoint.thread_id
                or not read.thread.turns or read.thread.turns[-1].id != checkpoint.turn_id
                or read.thread.turns[-1].status.value != "completed"):
            raise ValueError("A2A_CODEX_SESSION_NATIVE_TERMINAL_INVALID")
        bridge.register_thread(native.id)
        self._threads[native.id] = native
        return str(native.id)

    async def restore_managed_a2a_thread(self, recovery):
        """Restore a Kernel-validated expert checkpoint without starting a turn."""
        from pathlib import Path

        from ksadk.a2a.codex_recovery import CodexRecoveryCheckpoint
        from ksadk.codex.rollout_checkpoint import restore_recovery_rollout

        recovery = CodexRecoveryCheckpoint.model_validate(recovery.model_dump(by_alias=True))
        if recovery.continuation_started:
            raise ValueError("A2A_CODEX_RECOVERY_CONTINUATION_UNCERTAIN")
        bridge = getattr(self, "_a2a_expert_bridge", None)
        if (not self._checkpoint_home or bridge is None
                or recovery.bindings != {name: expert.spec for name, expert in bridge.experts.items()}):
            raise ValueError("A2A_CODEX_RECOVERY_BINDING_CONFLICT")
        if recovery.thread_id in self._threads or recovery.thread_id in self._active_handles:
            raise ValueError("A2A_CODEX_RECOVERY_THREAD_ALREADY_ATTACHED")
        native_record = await asyncio.to_thread(
            restore_recovery_rollout, Path(self._checkpoint_home), recovery.rollout,
            call_ids=tuple(call.call_id for call in recovery.calls),
        )
        config = dict(recovery.start_request.config)
        if recovery.start_request.model:
            config['model'] = recovery.start_request.model
        async with asyncio.timeout(10):
            if self._uses_manual_approval(config):
                native = await self._resume_manual_thread(recovery.thread_id, config)
            else:
                kwargs = thread_kwargs(config)
                kwargs.pop('ephemeral', None)
                native = await self._codex.thread_resume(recovery.thread_id, **kwargs)
            read = await native.read(include_turns=True)
        if native.id != recovery.thread_id or read.thread.id != recovery.thread_id:
            raise ValueError("A2A_CODEX_RECOVERY_NATIVE_IDENTITY_INVALID")
        turns = {turn.id: turn.status.value for turn in read.thread.turns}
        for call in recovery.calls:
            status = turns.get(call.turn_id)
            output = native_record.function_output(call.call_id)
            allowed = {"interrupted", "completed"} if output is not None else {"interrupted"}
            if status not in allowed:
                raise ValueError("A2A_CODEX_RECOVERY_NATIVE_TURN_NOT_INTERRUPTED")
        bridge.register_thread(native.id)
        self._threads[native.id] = native
        return str(native.id)


    async def _start_expert_thread(self, config):
        # Verified against the installed 0.154.0 app-server experimental schema:
        # its dynamicTools field is omitted from the public generated params.
        from openai_codex import ApprovalMode, AsyncThread
        from openai_codex._approval_mode import _approval_mode_settings
        from openai_codex._sandbox import _sandbox_mode
        from openai_codex.generated.v2_all import (
            ApprovalsReviewer, AskForApproval, AskForApprovalValue, ThreadStartParams,
        )
        if not self._codex._client._sync.config.experimental_api:
            raise ValueError("A2A_CODEX_EXPERIMENTAL_API_REQUIRED")
        kwargs = thread_kwargs(self._without_manual_approval(config))
        mode = kwargs.pop("approval_mode", ApprovalMode.auto_review)
        sandbox = kwargs.pop("sandbox", None)
        policy, reviewer = _approval_mode_settings(mode)
        if self._uses_manual_approval(config):
            policy, reviewer = AskForApproval(root=AskForApprovalValue.on_request), ApprovalsReviewer.user
        payload = ThreadStartParams(approval_policy=policy, approvals_reviewer=reviewer,
                                    sandbox=_sandbox_mode(sandbox), **kwargs).model_dump(mode="json", by_alias=True, exclude_none=True)
        payload["dynamicTools"] = self._a2a_expert_bridge.declarations()
        await self._codex._ensure_initialized()
        started = await self._codex._client.thread_start(payload)
        return AsyncThread(self._codex, started.thread.id)
