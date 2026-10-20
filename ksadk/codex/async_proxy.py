"""Select direct model providers or start the Codex model proxy."""

from __future__ import annotations

import os
import secrets
from typing import Any

from ksadk.codex import client as _client_module
from ksadk.codex.client import _CapabilityRoute, _is_openai_official, _upgrade_http_to_https


class _AsyncProxyMixin:
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
          - namespace/custom 支持 → 原生 Responses 直连
          - 仅 web_search 缺失 → 仍直连，并关闭该可选能力
          - namespace/custom 缺失或无法确认 → 自动启用代理
        - 凭证闭合:codex 子进程只拿随机 KSADK_PROXY_TOKEN;上游 key 留父进程。
        - 互斥:launch_args_override 已设时 raise(override 整体覆盖命令行)。
        - P1:直连分支(协议必需工具面确认、env=0)遇到自定义 base 也注入
          ``ksadk_direct`` provider——否则 codex 子进程回落默认 OpenAI 官方
          端点,自定义上游(OPENAI_API_BASE)静默失效。已显式设
          ``model_provider=`` 的 config 不覆盖;官方 base 不注入。

        返回 (新 config, ProxyServer | None)。staticmethod 便于单测。
        """

        from ksadk.codex.async_client import AsyncCodexClient
        runtime_env = {**os.environ, **(getattr(config, "env", None) or {})}
        env_val = runtime_env.get("KSADK_CODEX_USE_PROXY")
        if env_val in {"0", "direct"}:
            return AsyncCodexClient._inject_direct_provider(config), None
        if env_val in {"1", "forced"}:
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
        # Probe the exact URL scheme that Codex/proxy will use.  Managed
        # runtimes discover KSPMAS through its historical ``http://`` internal
        # URL, while the provider is upgraded to HTTPS before execution.  A
        # probe against HTTP can see only a redirect and incorrectly classify
        # an HTTPS ``/responses`` 404 as unknown, causing a broken direct path.
        probe_base = _upgrade_http_to_https(base)
        # Resolve through the protocol module so existing tests and callers
        # that patch the capability probe keep the same seam after the split.
        route = _client_module._probe_capability_route(model, probe_base, key)
        # Future required-tool declarations must be checked here before either
        # proxying or suppressing optional tools.
        if isinstance(route, bool):  # compatibility for injected test doubles
            route = _CapabilityRoute(use_proxy=route)
        route.require_available()
        if route.use_proxy:
            return AsyncCodexClient._start_proxy_and_inject(
                config,
                proxy_observer=proxy_observer,
            )
        # 探测确认协议必需工具面:直连,但必须把自定义 base 配成 provider。
        return (
            AsyncCodexClient._inject_direct_provider(
                config,
                disabled_tool_types=route.disabled_tool_types,
            ),
            None,
        )

    @staticmethod
    def _inject_direct_provider(
        config: Any,
        *,
        disabled_tool_types: frozenset[str] = frozenset(),
    ) -> Any:
        """直连模式注入 ``ksadk_direct`` provider(P1:非 proxy 不丢自定义 base)。

        - 无自定义 base / 官方 OpenAI base → 原样返回。
        - 已设 ``model_provider=`` → 保留 provider，仅追加必要的可选工具关闭项。
        - 否则追加 ``model_provider=ksadk_direct`` + base_url/env_key/wire_api
          (responses;该分支只在探测确认或显式强制直连时到达,其余走 proxy)。
        """
        import dataclasses

        from openai_codex import CodexConfig  # type: ignore[import-not-found]

        cfg = config if isinstance(config, CodexConfig) else CodexConfig()
        overrides = list(cfg.config_overrides or ())
        if any(str(o).startswith("model_provider=") for o in overrides):
            if "web_search" in disabled_tool_types and "web_search=disabled" not in overrides:
                return dataclasses.replace(
                    cfg,
                    config_overrides=tuple([*overrides, "web_search=disabled"]),
                )
            return config
        runtime_env = {**os.environ, **(cfg.env or {})}
        base = (
            runtime_env.get("KSADK_PROXY_UPSTREAM_BASE")
            or runtime_env.get("OPENAI_BASE_URL")
            or runtime_env.get("OPENAI_API_BASE")
            or ""
        )
        if not base or _is_openai_official(base):
            return config
        base = _upgrade_http_to_https(base)
        overrides += [
            "model_provider=ksadk_direct",
            "model_providers.ksadk_direct.name=ksadk_direct",
            f"model_providers.ksadk_direct.base_url={base}",
            "model_providers.ksadk_direct.env_key=OPENAI_API_KEY",
            "model_providers.ksadk_direct.wire_api=responses",
            "model_providers.ksadk_direct.supports_websockets=false",
            "model_providers.ksadk_direct.request_max_retries=2",
            "model_providers.ksadk_direct.stream_max_retries=2",
        ]
        if "web_search" in disabled_tool_types and "web_search=disabled" not in overrides:
            overrides.append("web_search=disabled")
        return dataclasses.replace(cfg, config_overrides=tuple(overrides))

    @staticmethod
    def _start_proxy_and_inject(
        config: Any,
        *,
        proxy_observer: Any = None,
        proxy_factory: Any,
        config_factory: Any,
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
        proxy = proxy_factory(
            config_factory(
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
        # 默认模型必须注入:codex 未配置 model= 时会发自家默认名(如 gpt-5.6-terra),
        # 上游(kspmas)按未知模型 403。注入后 thread 级 model 覆盖仍优先生效
        # (实测 codex thread model > config model=),RunAgent 的 Model 透传靠它。
        default_model = (
            runtime_env.get("OPENAI_MODEL_NAME") or runtime_env.get("MODEL_NAME") or ""
        ).strip()
        if default_model and not any(str(o).startswith("model=") for o in overrides):
            overrides.append(f"model={default_model}")
        overrides += [
            "model_provider=ksadk_proxy",
            "model_providers.ksadk_proxy.name=ksadk_proxy",
            f"model_providers.ksadk_proxy.base_url={proxy.base_url}",
            "model_providers.ksadk_proxy.env_key=KSADK_PROXY_TOKEN",
            "model_providers.ksadk_proxy.wire_api=responses",
            "model_providers.ksadk_proxy.supports_websockets=false",
            # Upstream failures arrive as Responses stream errors. Let the
            # native stream loop retry twice and publish its retry counters;
            # an additional HTTP retry loop would multiply attempts invisibly.
            "model_providers.ksadk_proxy.request_max_retries=0",
            "model_providers.ksadk_proxy.stream_max_retries=2",
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
