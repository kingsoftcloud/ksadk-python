"""A2A durable TaskStore (goal-05)。

契约(§4.6):托管被调 task 使用 SDK ``DatabaseTaskStore``,表名隔离为
``ksadk_a2a_tasks``,owner resolver 必须包含 account/runtime identity。
**不用 InMemoryTaskStore**——7/31 重启恢复发布门禁依赖 durable store。

注意:TaskStore 只持久化**协议 Task**,不等于 runner checkpoint;两者必须共同成功
才能声明 durable async(§7.2)。SQLite 只用于快速单测;进程崩溃恢复门禁必须使用
真实 PostgreSQL 和独立 OS 进程。
"""

from __future__ import annotations

import warnings
from threading import Lock
from typing import Any, Callable, Mapping, Optional, Sequence

from a2a.server import models as _sdk_models
from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import DatabaseTaskStore
from sqlalchemy.exc import SAWarning
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.requests import Request

from ksadk.a2a.identity import A2AIngressIdentity, A2ATrustedIdentityResolver

#: 契约 §4.6 规定的隔离表名。
A2A_TASK_TABLE = "ksadk_a2a_tasks"

DEFAULT_ACCOUNT_HEADERS = ("x-ksc-account-id", "x-account-id")
DEFAULT_RUNTIME_HEADERS = ("x-auth-agent-id", "x-ksc-agent-id", "x-runtime-id")


# a2a-sdk 1.1.0 dynamically declares a new ORM model for every custom-table
# store. Repeating the same table in one process raises an SQLAlchemy duplicate
# table error. Cache only KsADK's model instead of monkeypatching the SDK module.
_task_model_cache: dict[str, type[Any]] = {}
_task_model_lock = Lock()


def _task_model(table_name: str) -> type[Any]:
    with _task_model_lock:
        model = _task_model_cache.get(table_name)
        if model is None:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=(
                        "This declarative base already contains a class with the same class name"
                    ),
                    category=SAWarning,
                )
                model = _sdk_models.create_task_model(table_name)
            _task_model_cache[table_name] = model
        return model


class _KsADKDatabaseTaskStore(DatabaseTaskStore):
    """DatabaseTaskStore with a process-local, non-global custom-table model."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        create_table: bool,
        table_name: str,
        owner_resolver: Callable[[ServerCallContext], str],
    ) -> None:
        super().__init__(
            engine=engine,
            create_table=create_table,
            owner_resolver=owner_resolver,
        )
        self.task_model = _task_model(table_name)


def _first(state: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = state.get(key)
        if value:
            return str(value)
    return None


def default_owner_resolver(context: ServerCallContext) -> str:
    """默认 owner resolver:组合 account + runtime identity(契约 §4.6 要求两者)。

    owner 形如 ``"{account}/{runtime}"``:account 取 ``account_id``/``tenant_id``,
    runtime 取 ``runtime_id``/``agent_id``。**同一 account 下不同 runtime 形成独立任务
    权限域**(不会共用);只凑齐一侧时用一侧;两侧都缺时退回 ``user``/``anonymous``。
    生产环境应由鉴权/STS 在 context.state 注入这些键;本地无鉴权时退回匿名。
    """
    state = getattr(context, "state", None) or {}
    verified_identity = state.get(A2ATrustedIdentityResolver.state_key)
    if isinstance(verified_identity, A2AIngressIdentity):
        return verified_identity.owner_key()
    account = _first(state, "account_id", "tenant_id")
    runtime = _first(state, "runtime_id", "agent_id")
    parts = [part for part in (account, runtime) if part]
    if parts:
        return "/".join(parts)
    return _first(state, "user") or "anonymous"


class A2AOwnerContextBuilder(DefaultServerCallContextBuilder):
    """Build A2A call contexts from platform-authenticated HTTP identity.

    The SDK default stores headers only under ``state['headers']`` while the
    task store intentionally reads normalized top-level identity fields.  This
    builder is shared by JSON-RPC and REST so both transports enforce the same
    account/runtime owner boundary.  Deployments must strip/replace these
    platform headers at the public edge; message metadata is never trusted.
    """

    def __init__(
        self,
        *,
        account_headers: Sequence[str] = DEFAULT_ACCOUNT_HEADERS,
        runtime_headers: Sequence[str] = DEFAULT_RUNTIME_HEADERS,
        identity_resolver: A2ATrustedIdentityResolver | None = None,
        allow_unverified_identity: bool = True,
    ) -> None:
        self._account_headers = tuple(header.lower() for header in account_headers)
        self._runtime_headers = tuple(header.lower() for header in runtime_headers)
        self._identity_resolver = identity_resolver
        self._allow_unverified_identity = allow_unverified_identity

    @staticmethod
    def _header(headers: Mapping[str, Any], names: Sequence[str]) -> str | None:
        for name in names:
            value = str(headers.get(name) or "").strip()
            if value:
                return value
        return None

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        state = dict(context.state or {})
        headers = {str(key).lower(): value for key, value in request.headers.items()}
        scope_state = request.scope.get("state")
        trusted_state = dict(scope_state) if isinstance(scope_state, Mapping) else {}
        if self._identity_resolver is not None:
            identity = self._identity_resolver.resolve(request)
            state[A2ATrustedIdentityResolver.state_key] = identity
            state["account_id"] = identity.account_id
            state["tenant_id"] = identity.tenant_id
            state["caller_principal_type"] = identity.caller_principal_type
            state["caller_principal_id"] = identity.caller_principal_id
        elif not self._allow_unverified_identity:
            raise PermissionError("verified Gateway identity is required for inbound A2A")
        else:
            account = _first(trusted_state, "account_id", "tenant_id") or self._header(
                headers, self._account_headers
            )
            runtime = _first(trusted_state, "runtime_id", "agent_id") or self._header(
                headers, self._runtime_headers
            )
            if account:
                state["account_id"] = account
            if runtime:
                state["runtime_id"] = runtime
        context.state = state
        context.tenant = default_owner_resolver(context)
        return context


def build_a2a_task_store(
    dsn: str | None = None,
    *,
    engine: Optional[AsyncEngine] = None,
    table_name: str = A2A_TASK_TABLE,
    owner_resolver: Optional[Callable[[ServerCallContext], str]] = None,
    create_table: bool = True,
) -> DatabaseTaskStore:
    """构建 durable ``DatabaseTaskStore``。

    参数:
        dsn: 数据库 DSN(如 ``sqlite+aiosqlite:///path.db`` 或
            ``postgresql+asyncpg://...``)。与 ``engine`` 二选一。
        engine: 已存在的 SQLAlchemy AsyncEngine(优先于 dsn)。
        table_name: 隔离表名,默认 ``ksadk_a2a_tasks``。
        owner_resolver: owner 解析器,默认 :func:`default_owner_resolver`。
        create_table: 是否在 initialize 时建表(测试/本地默认 True;生产可 False
            由迁移管理)。
    """
    if engine is None:
        if not dsn:
            raise ValueError("build_a2a_task_store 需要 dsn 或 engine 之一")
        engine = create_async_engine(dsn)
    return _KsADKDatabaseTaskStore(
        engine=engine,
        create_table=create_table,
        table_name=table_name,
        owner_resolver=owner_resolver or default_owner_resolver,
    )


__all__ = [
    "A2A_TASK_TABLE",
    "A2AOwnerContextBuilder",
    "DEFAULT_ACCOUNT_HEADERS",
    "DEFAULT_RUNTIME_HEADERS",
    "build_a2a_task_store",
    "default_owner_resolver",
]
