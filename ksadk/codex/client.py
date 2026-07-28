"""CodexRuntime 的 codex 后端接口 (goal-09)。

重托管模式:CodexRuntime 对执行生命周期负责,不把 cancel/进程管理薄委托给上层。
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

import os
import secrets
from abc import ABC, abstractmethod
from importlib.metadata import PackageNotFoundError, version
from typing import Any, AsyncIterator, Optional

from ksadk.model_proxy import ProxyConfig, ProxyServer
from ksadk.model_proxy.cache import CapabilityCache
from ksadk.model_proxy.detect import probe_responses_capability

# 探测缓存单例:能力判定跨 client 共享,按 (model, base, credential_scope) 长缓存
_CAPABILITY_CACHE = CapabilityCache(ttl=3600)


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

    def probe(m: str, b: str, k: str):
        import httpx

        with httpx.Client() as client:
            return probe_responses_capability(client, b, k, m, timeout=15.0)

    caps = _CAPABILITY_CACHE.get_or_probe(model, base, key, probe)
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

    @abstractmethod
    async def interrupt_active_turn(self, thread_id: str) -> bool:
        """interrupt 该 thread 当前活跃 turn(真实 SDK ``handle.interrupt``);
        无活跃 turn 返回 False。"""
        raise NotImplementedError

    @abstractmethod
    async def resume_thread(self, thread_id: str, config: Optional[dict[str, Any]] = None) -> str:
        """恢复 thread(真实 SDK ``thread_resume``),返回 thread_id。"""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """释放后端资源(真实 SDK ``close``)。"""
        raise NotImplementedError


def _missing_codex_error() -> RuntimeError:
    return RuntimeError(
        "CodexRuntime 需要 openai-codex SDK;请安装 ksadk[codex]:"
        " pip install 'ksadk[codex]'(openai-codex 为可选 extra,不进默认依赖)"
    )


class AsyncCodexClient(CodexClient):
    """基于 OpenAI 官方 ``openai-codex`` SDK(``AsyncCodex``)的生产后端。

    **lazy import** ``openai_codex``:仅在实际使用时导入,缺依赖时显式报错
    (``pip install 'ksadk[codex]'``),不让所有 ksadk 用户默认背几十 MB CLI 二进制。
    构造时校验真实 SDK 方法存在(``thread_start``/``thread_resume``/``close`` 及
    ``AsyncThread.turn`` / ``AsyncTurnHandle.stream``/``interrupt``),版本漂移 fail-fast。
    """

    def __init__(self, config: Any = None) -> None:
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
        config, self._proxy = self._maybe_apply_proxy(config)
        self._codex = AsyncCodex(config=config)
        self.sdk_version = sdk_version
        self._threads: dict[str, Any] = {}  # thread_id -> AsyncThread
        self._active_handles: dict[str, Any] = {}  # thread_id -> 活跃 AsyncTurnHandle

    @staticmethod
    def _maybe_apply_proxy(config: Any) -> tuple[Any, Any]:
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
        env_val = os.environ.get("KSADK_CODEX_USE_PROXY")
        if env_val == "0":
            return config, None
        if env_val == "1":
            return AsyncCodexClient._start_proxy_and_inject(config)
        # 未设:智能探测
        base = (
            os.environ.get("KSADK_PROXY_UPSTREAM_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or ""
        )
        if not base or _is_openai_official(base):
            return config, None  # OpenAI 官方:直连,不探测
        model = os.environ.get("OPENAI_MODEL_NAME") or os.environ.get("MODEL_NAME") or ""
        key = os.environ.get("KSADK_PROXY_UPSTREAM_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        if _probe_requires_proxy(model, base, key):
            return AsyncCodexClient._start_proxy_and_inject(config)
        return config, None

    @staticmethod
    def _start_proxy_and_inject(config: Any) -> tuple[Any, Any]:
        """起进程内 ProxyServer + 注入 codex provider(opt-in/探测判定走代理时)。"""
        import dataclasses

        from openai_codex import CodexConfig  # type: ignore[import-not-found]

        cfg = config if isinstance(config, CodexConfig) else CodexConfig()
        if cfg.launch_args_override is not None:
            raise RuntimeError(
                "KSADK_CODEX_USE_PROXY 与 CodexConfig.launch_args_override 互斥:"
                "代理注入靠 config_overrides,而 launch_args_override 会整体覆盖命令行"
            )
        upstream = (
            os.environ.get("KSADK_PROXY_UPSTREAM_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or "https://kspmas.ksyun.com/v1"
        )
        upstream = _upgrade_http_to_https(upstream)
        api_key = (
            os.environ.get("KSADK_PROXY_UPSTREAM_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        )
        token = secrets.token_hex(16)
        proxy = ProxyServer(ProxyConfig(upstream_base=upstream, api_key=api_key, local_token=token))
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
        env = dict(cfg.env or {})
        env["KSADK_PROXY_TOKEN"] = token
        return dataclasses.replace(cfg, config_overrides=tuple(overrides), env=env), proxy


    async def start_thread(self, config: Optional[dict[str, Any]] = None) -> str:
        thread = await self._codex.thread_start(**self._thread_kwargs(config))
        self._threads[thread.id] = thread
        return str(thread.id)

    async def resume_thread(self, thread_id: str, config: Optional[dict[str, Any]] = None) -> str:
        # An ephemeral thread has no rollout on disk, so SDK thread_resume would
        # fail with -32600. Reuse its live AsyncThread for same-process resume.
        cached = self._threads.get(thread_id)
        if cached is not None:
            return str(cached.id)
        kwargs = self._thread_kwargs(config)
        # AsyncCodex.thread_resume 0.144.4 has no ephemeral parameter.
        kwargs.pop("ephemeral", None)
        thread = await self._codex.thread_resume(thread_id, **kwargs)
        self._threads[thread.id] = thread
        return str(thread.id)

    def run_turn(
        self,
        thread_id: str,
        prompt: Any,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._run_turn_gen(thread_id, prompt, config)

    async def _run_turn_gen(
        self, thread_id: str, prompt: Any, config: Optional[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        thread = self._threads.get(thread_id)
        if thread is None:
            # 未显式 start/resume 的 thread_id:按 resume 语义接入真实后端。
            await self.resume_thread(thread_id, config)
            thread = self._threads[thread_id]
        handle = await thread.turn(self._coerce_input(prompt), **self._turn_kwargs(config))
        self._active_handles[thread_id] = handle
        try:
            async for notification in handle.stream():
                event = self._notification_to_event_dict(notification)
                if event is not None:
                    yield event
        finally:
            self._active_handles.pop(thread_id, None)

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        handle = self._active_handles.get(thread_id)
        if handle is None:
            return False
        await handle.interrupt()
        return True

    async def close(self) -> None:
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
            "error",
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
