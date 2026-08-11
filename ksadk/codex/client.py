"""CodexRuntimeAdapter 的 codex 后端接口 (goal-09)。

重托管模式:CodexRuntimeAdapter 对执行生命周期负责,不把 cancel/进程管理薄委托给上层。
``CodexClient`` 是 codex 后端(thread/turn 生命周期)的最小接口,方法集对齐 OpenAI
官方 ``openai-codex`` SDK 的**真实**线程模型(``thread_start`` / ``thread.turn`` /
``handle.stream`` / ``handle.interrupt`` / ``thread_resume``):

- 生产实现 ``AsyncCodexClient``:基于 ``AsyncCodex``。**lazy import**,缺依赖时显式
  报"请安装 ksadk[codex]",不静默失败;构造时对真实 SDK 方法做 ``hasattr`` 校验,
  版本漂移时 fail-fast,而非运行期 AttributeError。
- 测试实现:用 fake(见 tests/runners/test_codex_runtime.py / test_adapter_contract.py),
  不需要真 CLI 二进制。

诚实边界:本模块的 SDK **方法面**已对安装的 ``openai-codex==0.144.4`` 实证(方法存在性 +
协程/asyncgen 形态);Notification → RuntimeEvent 的**字段级** phase 映射需在接真实 codex
后端时按实况对齐(结构已按生成的 payload 类型映射,见 ``_notification_to_event_dict``)。
"""

from __future__ import annotations

import asyncio
import os
import queue
import secrets
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from ksadk.model_proxy import ProxyConfig, ProxyServer
from ksadk.model_proxy.cache import CapabilityCache, credential_scope
from ksadk.model_proxy.detect import probe_responses_capability

# 探测缓存单例:能力判定跨 client 共享,按 (model, base, credential_scope) 长缓存
_CAPABILITY_CACHE = CapabilityCache(ttl=3600)


@dataclass
class _PendingApproval:
    approval_id: str
    thread_id: str
    method: str
    params: dict[str, Any]
    resolved: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PendingInteraction:
    interaction_id: str
    thread_id: str
    method: str
    params: dict[str, Any]
    resolved: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] = field(default_factory=dict)


def _is_openai_official(base: str) -> bool:
    """OpenAI 官方 base_url(直连,不探测不代理)。"""
    from urllib.parse import urlsplit

    host = (urlsplit(base).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.com")


def _upgrade_http_to_https(upstream: str) -> str:
    """http 非回环自动升级 https(凭证安全 + 兼容历史 http .env;星流等支持 https)。

    ProxyConfig 强制非回环 https(凭证不裸奔);历史 .env 常写 http://kspmas,
    这里 upgrade 让 codex 代理对它可用,不改通用模板(ADK 等用 http 本就 OK)。
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(upstream)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
    return upstream


def _probe_requires_proxy(model: str, base: str, key: str) -> bool:
    """探测上游:只有**确凿不支持 responses** 才返回 True(走代理)。

    supported/unknown 都返回 False(直连)。unknown(故障)保守直连——故障 ≠ 模型
    不支持 responses,不 silent 改变接入方式。结果经 CapabilityCache 缓存(singleflight)。
    """

    def probe(m: str, b: str):
        import httpx

        with httpx.Client() as client:
            return probe_responses_capability(client, b, key, m, timeout=15.0)

    caps = _CAPABILITY_CACHE.get_or_probe(model, base, credential_scope(key), probe)
    return caps.verdict == "unsupported"


class CodexClient(ABC):
    """codex 后端(thread/turn 生命周期)的最小接口(对齐真实 SDK 线程模型)。"""

    @abstractmethod
    async def start_thread(self, config: Optional[dict[str, Any]] = None) -> str:
        """新建 thread(真实 SDK ``thread_start``),返回 thread_id。"""
        raise NotImplementedError

    @abstractmethod
    def run_turn(
        self,
        thread_id: str,
        prompt: Any,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """在 thread 上跑一个 turn(``thread.turn`` + ``handle.stream``),
        产出规范化事件 dict(``{"method": ..., "params": ...}``)。"""
        raise NotImplementedError

    def run_goal(
        self,
        thread_id: str,
        objective: str,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one persisted Codex goal as a logical notification stream."""

        raise RuntimeError("connected Codex client does not support goal operations")

    @abstractmethod
    async def interrupt_active_turn(self, thread_id: str) -> bool:
        """interrupt 该 thread 当前活跃 turn(真实 SDK ``handle.interrupt``);
        无活跃 turn 返回 False。"""
        raise NotImplementedError

    @abstractmethod
    async def resume_thread(self, thread_id: str, config: Optional[dict[str, Any]] = None) -> str:
        """恢复 thread(真实 SDK ``thread_resume``),返回 thread_id。"""
        raise NotImplementedError

    async def resolve_approval(self, approval_id: str, decision: str) -> bool:
        """Resolve one live native approval request.

        Older/custom clients may not expose interactive approvals; returning
        ``False`` keeps that capability boundary explicit without breaking the
        existing client protocol.
        """

        return False

    async def resolve_interaction(
        self,
        interaction_id: str,
        data: dict[str, Any],
    ) -> bool:
        """Resolve one live structured user-input request."""

        return False

    async def pause_goal(self, thread_id: str) -> bool:
        """Pause an active native goal when supported."""

        return False

    async def cancel_goal(self, thread_id: str) -> bool:
        """Cancel an active native goal operation when supported."""

        return False

    @abstractmethod
    async def close(self) -> None:
        """释放后端资源(真实 SDK ``close``)。"""
        raise NotImplementedError


def _missing_codex_error() -> RuntimeError:
    return RuntimeError(
        "CodexRuntimeAdapter 需要 openai-codex SDK;请安装 ksadk[codex]:"
        " pip install 'ksadk[codex]'(openai-codex 为可选 extra,不进默认依赖)"
    )


class AsyncCodexClient(CodexClient):
    """基于 OpenAI 官方 ``openai-codex`` SDK(``AsyncCodex``)的生产后端。

    **lazy import** ``openai_codex``:仅在实际使用时导入,缺依赖时显式报错
    (``pip install 'ksadk[codex]'``),不让所有 ksadk 用户默认背几十 MB CLI 二进制。
    构造时校验真实 SDK 方法存在(``thread_start``/``thread_resume``/``close`` 及
    ``AsyncThread.turn`` / ``AsyncTurnHandle.stream``/``interrupt``),版本漂移 fail-fast。
    """

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

        # AsyncCodex 0.144.4 only accepts one CodexConfig positional/keyword.
        config, self._proxy = self._maybe_apply_proxy(
            config,
            proxy_observer=proxy_observer,
        )
        self._codex = AsyncCodex(config=config)
        self.sdk_version = sdk_version
        self._threads: dict[str, Any] = {}  # thread_id -> AsyncThread
        self._active_handles: dict[str, Any] = {}  # thread_id -> 活跃 AsyncTurnHandle
        self._goal_states: dict[str, Any] = {}
        self._approval_queues: dict[str, queue.Queue[Any]] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._pending_interactions: dict[str, _PendingInteraction] = {}
        self._approval_lock = threading.Lock()
        self._install_approval_bridge()

    def _install_approval_bridge(self) -> None:
        """Replace the SDK's unconditional accept handler with a HITL bridge.

        ``openai-codex==0.144.4`` exposes approval callbacks only on its sync
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
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return self._handle_approval_request(method, params)
        if method == "item/tool/requestUserInput":
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
            approval_queue.put(
                {
                    "method": "item/approval/requested",
                    "params": {
                        "id": approval_id,
                        "threadId": thread_id,
                        "kind": (
                            "command"
                            if method == "item/commandExecution/requestApproval"
                            else "file_change"
                        ),
                        "detail": raw,
                    },
                }
            )
        pending.resolved.wait()
        with self._approval_lock:
            self._pending_approvals.pop(approval_id, None)
        return pending.response or {"decision": "decline"}

    def _handle_user_input_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raw = dict(params or {})
        thread_id, request_queue = self._active_request_queue(raw)
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
        if request_queue is None:
            return {"answers": {}}

        surface_id = f"input-{interaction_id}"
        components: list[dict[str, Any]] = []
        question_components: list[dict[str, Any]] = []
        input_schema: dict[str, Any] = {"type": "object", "properties": {}}
        questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
        for index, raw_question in enumerate(questions):
            if not isinstance(raw_question, dict):
                continue
            question_id = str(raw_question.get("id") or f"question_{index + 1}")
            raw_options = raw_question.get("options")
            options = (
                [dict(option) for option in raw_options if isinstance(option, dict)]
                if isinstance(raw_options, list)
                else []
            )
            multiple = bool(
                raw_question.get("multiple")
                or raw_question.get("isMultiple")
                or raw_question.get("is_multiple")
                or raw_question.get("isMultiSelect")
                or raw_question.get("is_multi_select")
            )
            question_components.append(
                {
                    "id": question_id,
                    "component": "MultipleChoice",
                    "props": {
                        "name": question_id,
                        "label": str(
                            raw_question.get("header")
                            or raw_question.get("question")
                            or question_id
                        ),
                        "description": str(raw_question.get("question") or ""),
                        "options": options,
                        "multiple": multiple,
                        "allow_other": bool(
                            raw_question.get("isOther") or raw_question.get("is_other")
                        ),
                        "secret": bool(
                            raw_question.get("isSecret") or raw_question.get("is_secret")
                        ),
                    },
                }
            )
            option_labels = [
                str(option.get("label") or "") for option in options if option.get("label")
            ]
            input_schema["properties"][question_id] = (
                {
                    "type": "array",
                    "items": {"type": "string", "enum": option_labels},
                }
                if multiple
                else {"type": "string", "enum": option_labels}
            )
        components.append(
            {
                "id": "form",
                "component": "Form",
                "props": {"title": "需要你的反馈", "submit_label": "提交"},
                "children": question_components,
            }
        )
        with self._approval_lock:
            self._pending_interactions[interaction_id] = pending
            request_queue.put(
                {
                    "method": "a2ui/surface",
                    "params": {
                        "surface_id": surface_id,
                        "surface": {
                            "catalog_id": "https://a2ui.org/specification/v0_9/basic_catalog.json",
                            "components": components,
                            "data_model": {},
                        },
                    },
                }
            )
            request_queue.put(
                {
                    "method": "a2ui/interaction",
                    "params": {
                        "surface_id": surface_id,
                        "interaction_id": interaction_id,
                        "kind": "form",
                        "input_schema": input_schema,
                        "is_blocking": bool(raw.get("isBlocking", raw.get("is_blocking", True))),
                    },
                }
            )
        pending.resolved.wait()
        with self._approval_lock:
            self._pending_interactions.pop(interaction_id, None)
        return pending.response or {"answers": {}}

    @staticmethod
    def _maybe_apply_proxy(
        config: Any,
        *,
        proxy_observer: Any = None,
    ) -> tuple[Any, Any]:
        """codex 代理启用:**智能探测 fallback(带显式覆盖)**。

        - ``KSADK_CODEX_USE_PROXY=1`` → 强制开代理;``=0`` → 强制直连(可人工覆盖误判)。
        - **未设 env 时智能探测**:OpenAI 官方 base_url 直连(不探测);自定义上游
          (星流等)探测 responses 能力(detect.py + CapabilityCache 缓存,一次探测长缓存):
          - ``supported`` → 直连(原生 responses 可用)
          - ``unsupported`` → 自动启用代理(chat 模型,经转换层)
          - ``unknown``(故障/超时)→ **保守直连**,不 silent 改变接入方式
        - 凭证闭合:codex 子进程只拿随机 KSADK_PROXY_TOKEN;上游 key 留父进程。
        - 互斥:launch_args_override 已设时 raise(override 整体覆盖命令行)。

        返回 (新 config, ProxyServer | None)。staticmethod 便于单测。
        """
        runtime_env = {**os.environ, **(getattr(config, "env", None) or {})}
        env_val = runtime_env.get("KSADK_CODEX_USE_PROXY")
        if env_val == "0":
            return config, None
        if env_val == "1":
            return AsyncCodexClient._start_proxy_and_inject(
                config,
                proxy_observer=proxy_observer,
            )
        # 未设:智能探测
        base = (
            runtime_env.get("KSADK_PROXY_UPSTREAM_BASE")
            or runtime_env.get("OPENAI_BASE_URL")
            or runtime_env.get("OPENAI_API_BASE")
            or ""
        )
        if not base or _is_openai_official(base):
            return config, None  # OpenAI 官方:直连,不探测
        model = runtime_env.get("OPENAI_MODEL_NAME") or runtime_env.get("MODEL_NAME") or ""
        key = runtime_env.get("KSADK_PROXY_UPSTREAM_KEY") or runtime_env.get("OPENAI_API_KEY") or ""
        if _probe_requires_proxy(model, base, key):
            return AsyncCodexClient._start_proxy_and_inject(
                config,
                proxy_observer=proxy_observer,
            )
        return config, None

    @staticmethod
    def _start_proxy_and_inject(
        config: Any,
        *,
        proxy_observer: Any = None,
    ) -> tuple[Any, Any]:
        """起进程内 ProxyServer + 注入 codex provider(opt-in/探测判定走代理时)。"""
        import dataclasses

        from openai_codex import CodexConfig  # type: ignore[import-not-found]

        cfg = config if isinstance(config, CodexConfig) else CodexConfig()
        if cfg.launch_args_override is not None:
            raise RuntimeError(
                "KSADK_CODEX_USE_PROXY 与 CodexConfig.launch_args_override 互斥:"
                "代理注入靠 config_overrides,而 launch_args_override 会整体覆盖命令行"
            )
        runtime_env = {**os.environ, **(cfg.env or {})}
        upstream = (
            runtime_env.get("KSADK_PROXY_UPSTREAM_BASE")
            or runtime_env.get("OPENAI_BASE_URL")
            or runtime_env.get("OPENAI_API_BASE")
            or "https://kspmas.ksyun.com/v1"
        )
        upstream = _upgrade_http_to_https(upstream)
        api_key = (
            runtime_env.get("KSADK_PROXY_UPSTREAM_KEY") or runtime_env.get("OPENAI_API_KEY") or ""
        )
        token = secrets.token_hex(16)
        proxy = ProxyServer(
            ProxyConfig(
                upstream_base=upstream,
                api_key=api_key,
                local_token=token,
                upstream_model=runtime_env.get("OPENAI_MODEL_NAME")
                or runtime_env.get("MODEL_NAME")
                or "",
                event_callback=proxy_observer,
            )
        )
        proxy.start()
        overrides = list(cfg.config_overrides or ())
        overrides += [
            "model_provider=ksadk_proxy",
            "model_providers.ksadk_proxy.name=ksadk_proxy",
            f"model_providers.ksadk_proxy.base_url={proxy.base_url}",
            "model_providers.ksadk_proxy.env_key=KSADK_PROXY_TOKEN",
            "model_providers.ksadk_proxy.wire_api=responses",
            "model_providers.ksadk_proxy.supports_websockets=false",
            "web_search=disabled",
            "features.multi_agent=false",
            "features.multi_agent_v2=false",
        ]
        # The local proxy owns the upstream credential.  The Codex child only
        # receives a random loopback token, so prompt-driven commands cannot
        # read the real provider key from their environment.
        env = dict(runtime_env)
        for secret_name in (
            "OPENAI_API_KEY",
            "AGENTKIT_MODEL_API_KEY",
            "KSADK_PROXY_UPSTREAM_KEY",
        ):
            env.pop(secret_name, None)
        env["KSADK_PROXY_TOKEN"] = token
        return dataclasses.replace(cfg, config_overrides=tuple(overrides), env=env), proxy

    async def start_thread(self, config: Optional[dict[str, Any]] = None) -> str:
        if self._uses_manual_approval(config):
            thread = await self._start_manual_thread(config)
        else:
            thread = await self._codex.thread_start(**self._thread_kwargs(config))
        self._threads[thread.id] = thread
        return str(thread.id)

    async def resume_thread(self, thread_id: str, config: Optional[dict[str, Any]] = None) -> str:
        # An ephemeral thread has no rollout on disk, so SDK thread_resume would
        # fail with -32600. Reuse its live AsyncThread for same-process resume.
        cached = self._threads.get(thread_id)
        if cached is not None:
            return str(cached.id)
        if self._uses_manual_approval(config):
            thread = await self._resume_manual_thread(thread_id, config)
        else:
            kwargs = self._thread_kwargs(config)
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

        ``openai-codex==0.144.4`` exposes ``ApprovalsReviewer.user`` on the
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

        kwargs = self._thread_kwargs(self._without_manual_approval(config))
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

        kwargs = self._thread_kwargs(self._without_manual_approval(config))
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
        handle = await self._start_turn(thread, self._coerce_input(prompt), config)
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
        if thread_id not in self._threads:
            await self.resume_thread(thread_id, config)
        from openai_codex._goal import _AsyncGoalNotificationStream

        low_level = self._codex._client
        approval_queue: queue.Queue[Any] = queue.Queue()
        with self._approval_lock:
            self._approval_queues[thread_id] = approval_queue
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
            return await thread.turn(prompt, **self._turn_kwargs(config))
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

        turn_config = self._turn_kwargs(config)
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
        await handle.interrupt()
        return True

    async def pause_goal(self, thread_id: str) -> bool:
        state = self._goal_states.get(thread_id)
        if state is None:
            return False
        await self._codex._client.pause_goal(thread_id)
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
            pending.response = {"answers": answers}
            pending.resolved.set()
        return True

    async def close(self) -> None:
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
            interaction.response = {"answers": {}}
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

    # ---- 内部:config / input / 事件映射 ----

    @staticmethod
    def _thread_kwargs(config: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Map runtime config to the exact 0.144.4 thread API surface."""
        from openai_codex import ApprovalMode, Sandbox  # type: ignore[import-not-found]

        config = config or {}
        known = {
            "approval_mode",
            "model",
            "model_provider",
            "cwd",
            "sandbox",
            "service_tier",
            "base_instructions",
            "developer_instructions",
            "ephemeral",
        }
        result = {k: v for k, v in config.items() if k in known}
        # KSADK sessions are ephemeral so an interrupted/half-written turn cannot
        # later be revived from Codex's on-disk session store.
        result.setdefault("ephemeral", True)
        if config.get("sandbox_read_only", False):
            result["sandbox"] = Sandbox.read_only
            result.setdefault("approval_mode", ApprovalMode.deny_all)
        else:
            result["sandbox"] = _coerce_enum(Sandbox, result.get("sandbox"))
        result["approval_mode"] = _coerce_enum(ApprovalMode, result.get("approval_mode"))
        return {key: value for key, value in result.items() if value is not None}

    @staticmethod
    def _turn_kwargs(config: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Map runtime config to the exact 0.144.4 AsyncThread.turn surface."""
        from openai_codex import ApprovalMode, Sandbox  # type: ignore[import-not-found]

        config = config or {}
        known = {
            "approval_mode",
            "cwd",
            "effort",
            "model",
            "output_schema",
            "personality",
            "sandbox",
            "service_tier",
            "summary",
        }
        result = {key: value for key, value in config.items() if key in known}
        if config.get("sandbox_read_only", False):
            result["sandbox"] = Sandbox.read_only
            result.setdefault("approval_mode", ApprovalMode.deny_all)
        else:
            result["sandbox"] = _coerce_enum(Sandbox, result.get("sandbox"))
        result["approval_mode"] = _coerce_enum(ApprovalMode, result.get("approval_mode"))
        return {key: value for key, value in result.items() if value is not None}

    @staticmethod
    def _coerce_input(prompt: Any) -> Any:
        # RunInput 接受 str 或 [TextInput|...];str 直接透传。
        return prompt if isinstance(prompt, str) else prompt

    @staticmethod
    def _notification_to_event_dict(notification: Any) -> Optional[dict[str, Any]]:
        """把真实 ``Notification``(method + 类型化 payload)映射为运行时消费的规范化 dict。

        按 payload 类型路由(结构已对生成的 payload 类型实证);字段级 phase 细节在接
        真实后端时对齐。
        """
        payload = notification.payload
        if hasattr(payload, "model_dump"):
            params = payload.model_dump(mode="json")
        else:
            params = getattr(payload, "params", None)
            if not isinstance(params, dict):
                params = {}

        method = str(getattr(notification, "method", ""))
        supported_methods = {
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
            "item/autoApprovalReview/started",
            "item/autoApprovalReview/completed",
            "thread/tokenUsage/updated",
            "turn/started",
            "turn/completed",
            "error",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryPartAdded",
            "thread/goal/updated",
            "thread/goal/cleared",
        }
        if method in supported_methods:
            return {"method": method, "params": params}
        return None


def _coerce_enum(enum_type: Any, value: Any) -> Any:
    if value is None or isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(member.value for member in enum_type)
        raise ValueError(
            f"unsupported {enum_type.__name__} {value!r}; expected {supported}"
        ) from exc


__all__ = ["AsyncCodexClient", "CodexClient"]
