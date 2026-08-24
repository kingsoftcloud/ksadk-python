"""create_runtime_app — 普通 runtime 与 HarnessApp 共用的 app factory (goal-01)。

H2 §4.2:这是三线公共装配入口,替代 `ksadk/server/app.py` 的模块级单例 +
`set_runner` 全局态。纯结构性重构,不动业务逻辑。

设计要点:

- **per-app state**:RuntimeExecutor / RuntimeLaunchContext / detached-stream registry 全部挂在
  `app.state.runtime`(:class:`RuntimeAppState`)上,每个 app 实例一份,普通 app 与
  HarnessApp 互不共享。不再有模块级 ``runner`` / ``_DETACHED_*`` 全局可变态。
- **请求级桥接**:中间件把当前 app 的 state 写入 :data:`current_state` 的
  contextvar,handler 内既有辅助函数经 :func:`get_state` 取 state,从而**不必逐个
  改写约 30 个 handler 的签名**。后台 detached task 脱离请求上下文,在创建时
  从 state 捕获 registry(见 ``_detached_streaming_response``)。
- **route group 化**:路由按域拆成 APIRouter,factory 按 ``config.route_groups``
  可插拔装配;数据面 group 进 HarnessApp,控制面(cancel/resume/builder/debug)
  不进。
- **薄兼容壳**:`ksadk/server/app.py` 仍以 ``app`` / ``set_runner`` 暴露,内部
  改为调用本 factory,老调用方(TestClient / run_server / builders / deploy
  manager)零改动。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from fastapi import FastAPI

from ksadk.runtime.adapter import RuntimeAdapter, RuntimeLaunchContext
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.sandbox.registry import (
    SandboxRegistry,
    bind_sandbox_registry,
    set_fallback_sandbox_registry,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ksadk.server.terminal_sessions import TerminalSessionManager


# ---------------------------------------------------------------------------
# route group 注册表(冻结稿 G0.1):数据面 vs 控制面
# ---------------------------------------------------------------------------

#: 数据面 route group —— 可插拔装配进 HarnessApp。
DATA_PLANE_GROUPS: frozenset[str] = frozenset(
    {
        "health_meta",
        "sessions",
        "sessions_adk_compat",
        "run",
        "openai_compat",
        "workspace",
        "models",
        "feedback",
        "tools",
        "ui_bootstrap",
        "agui",
    }
)

#: 控制面 route group —— 不进入 HarnessApp(H2 §4.2:控制面 action 不进入)。
CONTROL_PLANE_GROUPS: frozenset[str] = frozenset(
    {
        "control",  # cancel / resume / checkpoint-resume-preview
        "builder",
        "debug",
    }
)

#: 全部 group(普通 runtime app 默认装配)。
ALL_GROUPS: frozenset[str] = DATA_PLANE_GROUPS | CONTROL_PLANE_GROUPS


class StreamRegistry:
    """per-app detached SSE stream + resume-key 状态。

    替代 `app.py` 曾经的模块级 ``_DETACHED_STREAMS`` / ``_DETACHED_STREAMS_BY_INVOCATION``
    / ``_DETACHED_RESUME_KEYS_BY_INVOCATION`` / ``_ACTIVE_DETACHED_RESUME_INVOCATION_BY_KEY``。
    生命周期绑定所属 app(lifespan 清理)。SSE/cancel 行为与旧模块级实现逐点一致。
    """

    def __init__(self) -> None:
        self.streams: set[asyncio.Task[Any]] = set()
        self.streams_by_invocation: dict[str, Any] = {}
        self.resume_keys_by_invocation: dict[str, tuple[str, str]] = {}
        self.active_resume_invocation_by_key: dict[tuple[str, str], str] = {}

    def clear(self) -> None:
        self.streams.clear()
        self.streams_by_invocation.clear()
        self.resume_keys_by_invocation.clear()
        self.active_resume_invocation_by_key.clear()


class RuntimeAppState:
    """per-app 运行时状态及其拥有的可清理资源。"""

    def __init__(
        self,
        *,
        executor: RuntimeExecutor | None = None,
        launch_context: RuntimeLaunchContext | None = None,
        session_service_provider: Callable[[], Any] | None = None,
        session_backend_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.app: Optional[FastAPI] = None
        self.executor = executor
        self.launch_context = launch_context
        self.stream_registry = StreamRegistry()
        self.sandbox_registry = SandboxRegistry()
        self.session_service: Any = None
        self._session_services_by_loop: dict[asyncio.AbstractEventLoop, Any] = {}
        self._sync_session_service: Any = None
        # The legacy ``server.app`` facade exposes monkeypatchable providers.
        # New factory callers leave these unset and get per-app owned services.
        self._session_service_provider = session_service_provider
        self._session_backend_provider = session_backend_provider
        # app.py 在装配 terminal routes 时创建，避免 factory 反向依赖 server。
        self.terminal_manager: Optional[TerminalSessionManager] = None
        # A2A 装配产物(config.a2a.enabled 时由 _wire_a2a_if_enabled 写入)。
        self.a2a_server: Any = None
        self.a2a_bootstrap: Any = None
        # AG-UI endpoint 及其 app-owned RuntimeAdapter handle registry。
        self.agui_agent: Any = None
        self.agui_config: Any = None

    def resolve_session_service(self) -> Any:
        """Return this app's session service for the current execution loop."""
        if self.session_service is not None:
            return self.session_service
        if self._session_service_provider is not None:
            return self._session_service_provider()

        from ksadk.sessions import create_session_service

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._sync_session_service is None:
                self._sync_session_service = create_session_service()
            return self._sync_session_service

        service = self._session_services_by_loop.get(loop)
        if service is None:
            service = create_session_service()
            self._session_services_by_loop[loop] = service
        return service

    def session_services(self) -> list[Any]:
        """Return all unique services owned by this app for shutdown."""
        candidates = [
            self.session_service,
            self._sync_session_service,
            *self._session_services_by_loop.values(),
        ]
        services: list[Any] = []
        for service in candidates:
            if service is not None and all(service is not item for item in services):
                services.append(service)
        return services

    def describe_session_backend(self) -> dict[str, Any]:
        if self._session_backend_provider is not None:
            return dict(self._session_backend_provider())
        from ksadk.sessions import describe_session_backend

        return dict(describe_session_backend())


class RuntimeAppConfig:
    """create_runtime_app 的装配配置。

    - ``runtime_executor``:统一 Runtime 生命周期入口。
    - ``launch_context``:当前 Runtime 与项目的不可变启动上下文。
    - ``runtime_type``:runtime 类型标识(普通 / harness / codex ...)。
    - ``route_groups``:要装配的 route group 集合;默认 :data:`ALL_GROUPS`,
      HarnessApp 传 :data:`DATA_PLANE_GROUPS`。
    """

    def __init__(
        self,
        *,
        runtime_type: str = "local",
        route_groups: Optional[set[str]] = None,
        a2a: Optional[Any] = None,
        a2a_runtime_adapter: RuntimeAdapter | None = None,
        agui: Optional[Any] = None,
        runtime_executor: RuntimeExecutor | None = None,
        launch_context: RuntimeLaunchContext | None = None,
        kernel_adapter_provider: Callable[[], RuntimeAdapter] | None = None,
        session_service_provider: Callable[[], Any] | None = None,
        session_backend_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.runtime_type = runtime_type
        self.route_groups: set[str] = (
            set(route_groups) if route_groups is not None else set(ALL_GROUPS)
        )
        # A2A 协议装配配置(``ksadk.a2a.routes.A2AConfig``);enabled 时 factory 装配
        # A2A 数据面端点(契约 §8)。用 Any 避免本模块硬依赖可选的 a2a-sdk。
        self.a2a = a2a
        self.a2a_runtime_adapter = a2a_runtime_adapter
        self.agui = agui
        self.runtime_executor = runtime_executor
        self.launch_context = launch_context
        # 特殊 runtime 可显式提供 worker/recovery 的 adapter；常规部署从
        # runtime_executor 的同一 registry 派生，见 _kernel_adapter_provider。
        self.kernel_adapter_provider = kernel_adapter_provider
        self.session_service_provider = session_service_provider
        self.session_backend_provider = session_backend_provider


# ---------------------------------------------------------------------------
# 请求级 state 桥接
# ---------------------------------------------------------------------------

_current_state: contextvars.ContextVar[Optional[RuntimeAppState]] = contextvars.ContextVar(
    "ksadk_runtime_app_state", default=None
)

# 非请求上下文(直接调用内部 helper 的测试 / run_server 启动前)的兜底 state。
# 由 app.py 在 factory 建 app 后指向 ``app.state.runtime``。
_fallback_state: RuntimeAppState = RuntimeAppState()


def set_fallback_state(state: RuntimeAppState) -> None:
    """把兜底 state 指向某个 app 的 state(兼容壳用)。"""
    global _fallback_state
    _fallback_state = state
    set_fallback_sandbox_registry(state.sandbox_registry)


def get_state() -> RuntimeAppState:
    """取当前请求对应的 app state;非请求上下文退回兜底 state。"""
    state = _current_state.get()
    return state if state is not None else _fallback_state


def get_runtime_execution() -> tuple[RuntimeExecutor, RuntimeLaunchContext]:
    """Resolve the current app's canonical runtime execution dependencies."""

    from fastapi import HTTPException

    state = get_state()
    if state.executor is None or state.launch_context is None:
        raise HTTPException(status_code=500, detail="RuntimeExecutor 未初始化")
    return state.executor, state.launch_context


@contextmanager
def bind_runtime_state(state: RuntimeAppState) -> Iterator[None]:
    """Bind one app state for non-HTTP scopes such as WebSockets."""
    token = _current_state.set(state)
    try:
        with bind_sandbox_registry(state.sandbox_registry):
            yield
    finally:
        _current_state.reset(token)


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

# configure 回调签名:由 app.py 提供,负责把各 domain router 挂到 app 上。
ConfigureApp = Callable[[FastAPI, RuntimeAppState, set[str]], None]


async def shutdown_runtime_resources(state: RuntimeAppState) -> None:
    """Close only resources owned by one runtime app."""
    manager = state.terminal_manager
    if manager is not None:
        try:
            await manager.close()
        except Exception:
            logger.exception("failed to close terminal sessions on shutdown")

    registry = state.stream_registry
    pending_streams = list(registry.streams)
    for task in pending_streams:
        task.cancel()
    if pending_streams:
        await asyncio.gather(*pending_streams, return_exceptions=True)
    registry.clear()

    if state.executor is not None:
        try:
            await state.executor.close_all()
        except Exception:
            logger.exception("failed to close runtime adapters on shutdown")

    for service in state.session_services():
        close = getattr(service, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                logger.exception("failed to close app session service on shutdown")

    try:
        state.sandbox_registry.close()
    except Exception:
        logger.exception("failed to clear sandbox registry on shutdown")


def _is_agent_execution_path(path: str, method: str) -> bool:
    """是否 agent 执行类请求(需要 root span 兜底,让 tool 内 outbound 挂到该 trace)。

    session 管理(GetAgentUiBootstrap/ListSessionMessages 等轮询)与 UI/health 不建 span。
    """
    m = (method or "").upper()
    p = (path or "").strip()
    if m == "POST" and p in {
        "/v1/responses",
        "/v1/chat/completions",
        "/run",
        "/run_sse",
        "/agentengine/api/v1/RunAgent",
    }:
        return True
    if m == "GET" and p in {"/agentengine/api/v1/SubscribeRunEvents"}:
        return True
    return False


def create_runtime_app(
    config: RuntimeAppConfig,
    configure: Optional[ConfigureApp] = None,
) -> FastAPI:
    """装配一个 runtime app。

    参数:
        config: :class:`RuntimeAppConfig`(executor / launch_context / route_groups)。
        configure: 路由装配回调 ``configure(app, state, route_groups)``;由
            ``ksadk/server/app.py`` 提供,负责按 group 把 domain router include 进 app。
            factory 自身只负责 FastAPI 实例、state、中间件、lifespan、异常处理器,
            不 import 全局 ``base_app`` 再过滤复制路由(H2 禁止)。
    """
    from contextlib import asynccontextmanager
    from typing import AsyncIterator

    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import Response

    state = RuntimeAppState(
        executor=config.runtime_executor,
        launch_context=config.launch_context,
        session_service_provider=config.session_service_provider,
        session_backend_provider=config.session_backend_provider,
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 生产 kernel 的 composition root 必须启动 worker/lease/recovery，
        # 不能只注册一个可接收命令、却永远不会消费的 ingress kernel。
        from ksadk.kernel import ingress as _kernel_ingress
        from ksadk.kernel.bootstrap import (
            bootstrap_agent_kernel_runtime_from_env,
            clear_agent_kernel_runtime,
        )
        from ksadk.runtime.factory import kernel_start_request_defaults

        adapter_provider = _kernel_adapter_provider(config)
        request_defaults = (
            kernel_start_request_defaults(config.launch_context)
            if config.launch_context is not None
            else {}
        )
        kernel_runtime = await bootstrap_agent_kernel_runtime_from_env(
            adapter_provider=adapter_provider,
            runtime_executor=config.runtime_executor,
            launch_context=config.launch_context,
            start_request_defaults=request_defaults,
        )
        app.state.agent_kernel_runtime = kernel_runtime
        try:
            if state.a2a_bootstrap is not None:
                await state.a2a_bootstrap.start()
            yield
        finally:
            if state.a2a_bootstrap is not None:
                await state.a2a_bootstrap.stop()
            if kernel_runtime is not None:
                await kernel_runtime.close()
            clear_agent_kernel_runtime()
            _kernel_ingress.clear_agent_kernel()
            await shutdown_runtime_resources(state)

    app = FastAPI(
        title="ADK Core API",
        description="Agent Development Kit HTTP API",
        version="1.0.0",
        lifespan=_lifespan,
    )
    state.app = app
    app.state.runtime = state

    # 中间件:把当前 app 的 state 写入请求级 contextvar(handler 经 get_state() 取)。
    @app.middleware("http")
    async def _runtime_state_bridge(request, call_next):
        from ksadk.server.routes.dependencies import bind_session_service

        with (
            bind_runtime_state(state),
            bind_session_service(
                state.resolve_session_service(),
                backend=state.describe_session_backend(),
            ),
        ):
            # 提取 inbound OTel trace context(traceparent);无有效 inbound 时建一个
            # 覆盖整个请求的 root server span,让 langchain/openinference 的 agent span 与
            # tool 内 outbound(A2A)调用都挂到这条 trace 上(openinference 只在 LLM 调用
            # 期间建 span,tool 执行时其 span 已 detach,需一个贯穿 span 兜底)。
            # 只对 agent 执行类路径建 root span;session/UI 管理路径(GetAgentUiBootstrap/
            # ListSessionMessages 等轮询)不建,避免一次问答产生一堆独立 trace。
            try:
                from opentelemetry import context as _otel_ctx
                from opentelemetry import propagate
                from opentelemetry import trace as _otel_trace

                _carrier = dict(request.headers)
                _parent = propagate.extract(_carrier)
                _parent_span_ctx = _otel_trace.get_current_span(_parent).get_span_context()
            except Exception:
                logger.exception("failed to initialize request tracing; continuing without OTel")
                response = await call_next(request)
            else:
                if _parent_span_ctx.is_valid:
                    try:
                        _token = _otel_ctx.attach(_parent)
                    except Exception:
                        logger.exception(
                            "failed to attach inbound trace context; continuing without OTel"
                        )
                        response = await call_next(request)
                    else:
                        try:
                            response = await call_next(request)
                        finally:
                            try:
                                _otel_ctx.detach(_token)
                            except Exception:
                                logger.exception("failed to detach inbound trace context")
                elif _is_agent_execution_path(request.url.path, request.method):
                    try:
                        _tracer = _otel_trace.get_tracer("ksadk.server")
                        _span_scope = _tracer.start_as_current_span(
                            f"{request.method} {request.url.path}"
                        )
                        _span_scope.__enter__()
                    except Exception:
                        logger.exception(
                            "failed to start request span; continuing without OTel"
                        )
                        response = await call_next(request)
                    else:
                        try:
                            response = await call_next(request)
                        except BaseException as exc:
                            try:
                                _span_scope.__exit__(type(exc), exc, exc.__traceback__)
                            except Exception:
                                logger.exception("failed to close request span after handler error")
                            raise
                        else:
                            try:
                                _span_scope.__exit__(None, None, None)
                            except Exception:
                                logger.exception("failed to close request span")
                else:
                    response = await call_next(request)
            path = request.url.path
            if path == "/" or path.endswith(".html"):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

    # CORS(与旧 app 一致,默认对 ADK 工具放开)。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 异常处理器(与旧 app 一致)。
    import json as _json

    from ksadk.sessions.errors import SessionBackendUnavailable

    @app.exception_handler(SessionBackendUnavailable)
    async def session_backend_unavailable_handler(_request, exc: SessionBackendUnavailable):
        return Response(
            content=_json.dumps(
                {"detail": {"code": "session_backend_unavailable", "message": str(exc)}},
                ensure_ascii=False,
            ),
            status_code=503,
            media_type="application/json",
        )

    # A2A 端点须在 configure 之前装配:health_meta 含 GET catch-all ``/{requested_path:path}``,
    # 若 A2A 后挂,A2A 的 GET 路由(agent-card / tasks/{id})会被 catch-all 遮蔽成 404。
    _wire_a2a_if_enabled(app, state, config)
    _wire_agui_if_enabled(app, state, config)

    if configure is not None:
        configure(app, state, set(config.route_groups))

    return app


def _kernel_adapter_provider(
    config: RuntimeAppConfig,
) -> Callable[[], RuntimeAdapter] | None:
    """Return the adapter source used by the durable kernel runtime.

    Hosted startup deliberately fails when neither a provider nor a Runtime
    launch context is available. It must never construct an unrelated local
    adapter merely to make the ingress look healthy.
    """

    if config.kernel_adapter_provider is not None:
        return config.kernel_adapter_provider
    if config.a2a_runtime_adapter is not None:
        return lambda: config.a2a_runtime_adapter
    if config.runtime_executor is not None and config.launch_context is not None:
        return lambda: config.runtime_executor.create_adapter(config.launch_context)
    return None


def _wire_a2a_if_enabled(app: FastAPI, state: RuntimeAppState, config: RuntimeAppConfig) -> None:
    """Mount A2A with an explicitly injected RuntimeAdapter.

    The server composition root never resolves or wraps a Runner.  Framework
    internals may still use one behind their adapter factory, but A2A only sees
    the frozen RuntimeAdapter contract.
    """
    a2a_cfg = config.a2a
    if a2a_cfg is None:
        return
    from ksadk.managed_a2a_card import ManagedA2ACardMount

    if isinstance(a2a_cfg, ManagedA2ACardMount):
        a2a_cfg.mount(app)
        state.a2a_bootstrap = a2a_cfg
        logger.info(
            "managed A2A discovery card mounted(agent_name=%s base_url=%s)",
            a2a_cfg.config.agent_name,
            a2a_cfg.config.base_url,
        )
        return
    from ksadk.a2a.bootstrap import AgentEngineA2ABootstrap

    adapter = config.a2a_runtime_adapter
    if adapter is None:
        raise ValueError("A2A requires an explicitly injected RuntimeAdapter")
    runtime_type = adapter.runtime.runtime_type or config.runtime_type

    if isinstance(a2a_cfg, AgentEngineA2ABootstrap):
        server = a2a_cfg.mount(
            app,
            runtime_adapter=adapter,
            runtime_type=runtime_type,
        )
        state.a2a_bootstrap = a2a_cfg
        state.a2a_server = server
        logger.info(
            "managed A2A Runtime mounted(agent=%s inbound_enabled=%s)",
            a2a_cfg.runtime_metadata.agent_id,
            a2a_cfg.inbound_enabled,
        )
        return
    if not getattr(a2a_cfg, "enabled", False):
        return
    from ksadk.a2a.routes import add_a2a_protocol_routes
    from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter

    task_adapter = A2ARuntimeTaskAdapter(adapter, runtime_type=runtime_type)
    server = add_a2a_protocol_routes(app, a2a_cfg, task_adapter=task_adapter)
    state.a2a_server = server
    logger.info("A2A 协议端点已装配进 runtime app(agent=%s)", a2a_cfg.agent_name)


def _wire_agui_if_enabled(app: FastAPI, state: RuntimeAppState, config: RuntimeAppConfig) -> None:
    """Mount the optional official AG-UI endpoint before the static catch-all."""
    agui_cfg = config.agui
    if (
        agui_cfg is None
        or not getattr(agui_cfg, "enabled", False)
        or "agui" not in config.route_groups
    ):
        return

    from ksadk.agui.config import require_agui_dependencies

    require_agui_dependencies()

    from ksadk.agui.routes import add_ksadk_agui_endpoint
    from ksadk.events.store import RuntimeEventStore
    from ksadk.server.routes import dependencies as route_dependencies

    if state.executor is None or state.launch_context is None:
        raise ValueError("AG-UI requires RuntimeExecutor and RuntimeLaunchContext")

    class _AutoSessionRuntimeEventStore:
        """Create a session on first AG-UI event when HttpAgent starts fresh."""

        def __init__(self, service: Any) -> None:
            self._service = service
            self._store = RuntimeEventStore(service)

        async def append_one(self, session_id: str, event: Any) -> Any:
            existing = await self._service.get_session(session_id)
            if existing is None:
                metadata = getattr(event, "source", None)
                meta = metadata.metadata if metadata is not None else {}
                await self._service.create_session(
                    str(meta.get("agent_id") or "agent"),
                    str(meta.get("user_id") or "user"),
                    session_id,
                )
            return await self._store.append_one(session_id, event)

        async def reserve_once(self, event: Any) -> Any:
            existing = await self._service.get_session(event.session_id)
            if existing is None:
                await self._service.create_session(
                    event.agent_id,
                    event.user_id,
                    event.session_id,
                )
            return await self._store.reserve_once(event)

        async def list(self, session_id: str, **kwargs: Any) -> Any:
            return await self._store.list(session_id, **kwargs)

    agent = add_ksadk_agui_endpoint(
        app,
        state.executor,
        state.launch_context,
        agui_cfg,
        event_store_factory=lambda: _AutoSessionRuntimeEventStore(
            route_dependencies.resolve_session_service()
        ),
        session_service_factory=route_dependencies.resolve_session_service,
    )
    state.agui_agent = agent
    state.agui_config = agui_cfg
    logger.info("AG-UI endpoint mounted at %s", agui_cfg.path)


__all__ = [
    "ALL_GROUPS",
    "CONTROL_PLANE_GROUPS",
    "DATA_PLANE_GROUPS",
    "RuntimeAppConfig",
    "RuntimeAppState",
    "bind_runtime_state",
    "StreamRegistry",
    "create_runtime_app",
    "get_runtime_execution",
    "get_state",
    "set_fallback_state",
]
