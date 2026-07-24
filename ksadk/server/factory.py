"""create_runtime_app — 普通 runtime 与 HarnessApp 共用的 app factory (goal-01)。

H2 §4.2:这是三线公共装配入口,替代 `ksadk/server/app.py` 的模块级单例 +
`set_runner` 全局态。纯结构性重构,不动业务逻辑。

设计要点:

- **per-app state**:runner / runner_loaded / detached-stream registry 全部挂在
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
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional, cast

from fastapi import FastAPI

from ksadk.runners.base_runner import BaseRunner
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
        runner: Optional[BaseRunner] = None,
        *,
        session_service_provider: Callable[[], Any] | None = None,
        session_backend_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.app: Optional[FastAPI] = None
        self.runner: Optional[BaseRunner] = runner
        self.runner_loaded: bool = False
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

    - ``runner``:依赖注入的 runner(替代 set_runner 全局态);可在装配后由
      兼容壳 ``set_runner`` 再写入 ``app.state.runtime.runner``。
    - ``runtime_type``:runtime 类型标识(普通 / harness / codex ...)。
    - ``route_groups``:要装配的 route group 集合;默认 :data:`ALL_GROUPS`,
      HarnessApp 传 :data:`DATA_PLANE_GROUPS`。
    """

    def __init__(
        self,
        runner: Optional[BaseRunner] = None,
        *,
        runtime_type: str = "local",
        route_groups: Optional[set[str]] = None,
        a2a: Optional[Any] = None,
        agui: Optional[Any] = None,
        session_service_provider: Callable[[], Any] | None = None,
        session_backend_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.runner = runner
        self.runtime_type = runtime_type
        self.route_groups: set[str] = (
            set(route_groups) if route_groups is not None else set(ALL_GROUPS)
        )
        # A2A 协议装配配置(``ksadk.a2a.routes.A2AConfig``);enabled 时 factory 装配
        # A2A 数据面端点(契约 §8)。用 Any 避免本模块硬依赖可选的 a2a-sdk。
        self.a2a = a2a
        self.agui = agui
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


@contextmanager
def bind_runtime_state(state: RuntimeAppState) -> Iterator[None]:
    """Bind one app state for non-HTTP scopes such as WebSockets."""
    token = _current_state.set(state)
    try:
        with bind_sandbox_registry(state.sandbox_registry):
            yield
    finally:
        _current_state.reset(token)


def get_runner() -> BaseRunner:
    """取当前 app 的 runner(懒加载 agent)。等价旧的 ``_resolve_active_runner``。"""
    from fastapi import HTTPException

    state = get_state()
    runner = state.runner
    if runner is None:
        raise HTTPException(status_code=500, detail="Runner 未初始化")
    if not state.runner_loaded:
        try:
            runner.load_agent()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Runner 加载失败: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc) or "Runner 加载失败") from exc
        state.runner_loaded = True
    return runner


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

    active_runner = state.runner
    if active_runner is not None:
        close = getattr(active_runner, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                logger.exception("failed to close runner on shutdown")

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


def create_runtime_app(
    config: RuntimeAppConfig,
    configure: Optional[ConfigureApp] = None,
) -> FastAPI:
    """装配一个 runtime app。

    参数:
        config: :class:`RuntimeAppConfig`(runner / runtime_type / route_groups)。
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
        runner=config.runner,
        session_service_provider=config.session_service_provider,
        session_backend_provider=config.session_backend_provider,
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
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
            # 保持旧行为:前端入口禁缓存。
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


class _LazyRunnerProxy:
    """把 app.state 的 runner 以惰性方式暴露给 A2A executor/adapter。

    兼容壳常在 factory 建 app 后才 ``set_runner`` 写入真实 runner,因此不能在装配期
    固化 runner 引用。代理在每次调用时从 ``state`` 解析真实 runner 并懒加载,
    接口与 ``BaseRunner`` 对齐(load_agent / stream / invoke,其余属性经 __getattr__
    转发)。
    """

    def __init__(self, state: RuntimeAppState) -> None:
        self.__dict__["_state"] = state

    def _real(self) -> BaseRunner:
        state: RuntimeAppState = self.__dict__["_state"]
        runner = state.runner
        if runner is None:
            raise RuntimeError("A2A: runner 尚未装配(set_runner 未调用)")
        if not state.runner_loaded:
            runner.load_agent()
            state.runner_loaded = True
        return runner

    def load_agent(self) -> BaseRunner:
        return self._real()

    def stream(self, input_data: Any) -> Any:
        # 与普通方法(非 async def)返回真实 runner.stream 的结果(async gen 或 coroutine),
        # 由 RunnerRuntimeAdapter/executor 按既有分支处理。
        return self._real().stream(input_data)

    async def invoke(self, input_data: Any) -> Any:
        return await self._real().invoke(input_data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real(), name)


def _wire_a2a_if_enabled(app: FastAPI, state: RuntimeAppState, config: RuntimeAppConfig) -> None:
    """契约 §8:``config.a2a.enabled`` 时把 A2A 协议端点装配进 app(数据面)。

    runner 经 :class:`_LazyRunnerProxy` 惰性解析;RuntimeAdapter 用通用
    ``RunnerRuntimeAdapter``(经 P0-2 后 cancel 走真实 asyncio 任务中断)。
    """
    a2a_cfg = config.a2a
    if a2a_cfg is None or not getattr(a2a_cfg, "enabled", False):
        return
    from ksadk.a2a.routes import add_a2a_protocol_routes
    from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

    proxy = _LazyRunnerProxy(state)
    adapter = RunnerRuntimeAdapter(cast("BaseRunner", proxy), runtime_type=config.runtime_type)
    task_adapter = A2ARuntimeTaskAdapter(adapter, runtime_type=config.runtime_type)
    server = add_a2a_protocol_routes(app, proxy, a2a_cfg, task_adapter=task_adapter)
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

    proxy = _LazyRunnerProxy(state)

    class _AutoSessionRuntimeEventStore:
        """Create a session on first AG-UI event when HttpAgent starts fresh."""

        def __init__(self, service: Any) -> None:
            self._service = service
            self._store = RuntimeEventStore(service)

        async def append_one(self, event: Any) -> Any:
            existing = await self._service.get_session(event.session_id)
            if existing is None:
                await self._service.create_session(
                    event.agent_id,
                    event.user_id,
                    event.session_id,
                )
            return await self._store.append_one(event)

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
        cast("BaseRunner", proxy),
        agui_cfg,
        event_store_factory=lambda: _AutoSessionRuntimeEventStore(
            route_dependencies.resolve_session_service()
        ),
        session_service_factory=route_dependencies.resolve_session_service,
    )
    state.agui_agent = agent
    state.agui_config = agui_cfg
    logger.info("AG-UI endpoint mounted at %s", agui_cfg.path)


def wire_default_agui_for_runner(state: RuntimeAppState, runner: BaseRunner) -> None:
    """Mount AG-UI for legacy ``app`` + ``set_runner`` production entrypoints.

    Those entrypoints create the FastAPI app before the framework runner exists.
    Mounting immediately after ``set_runner`` is still startup-time wiring.  New
    protocol routes are moved ahead of the static catch-all to preserve routing
    order exactly as the normal factory path does.
    """
    if state.app is None or state.agui_agent is not None:
        return

    from ksadk.agui.config import default_agui_config

    agui_config = default_agui_config(runner)
    if not agui_config.enabled:
        return

    app = state.app
    routes = app.router.routes
    previous_count = len(routes)
    _wire_agui_if_enabled(
        app,
        state,
        RuntimeAppConfig(
            runner=runner,
            runtime_type=agui_config.runtime_type,
            route_groups={"agui"},
            agui=agui_config,
        ),
    )
    new_routes = routes[previous_count:]
    if not new_routes:
        return
    del routes[previous_count:]
    catch_all_index = next(
        (
            index
            for index, route in enumerate(routes)
            if getattr(route, "path", None) == "/{requested_path:path}"
        ),
        len(routes),
    )
    routes[catch_all_index:catch_all_index] = new_routes


__all__ = [
    "ALL_GROUPS",
    "CONTROL_PLANE_GROUPS",
    "DATA_PLANE_GROUPS",
    "RuntimeAppConfig",
    "RuntimeAppState",
    "bind_runtime_state",
    "StreamRegistry",
    "create_runtime_app",
    "get_runner",
    "get_state",
    "set_fallback_state",
    "wire_default_agui_for_runner",
]
