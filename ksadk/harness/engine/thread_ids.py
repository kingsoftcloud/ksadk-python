"""Checkpointer thread_id 租户复合编码（plan §6.2.2，Phase 1 决策）。

LangGraph Checkpointer 的 ``thread_id`` 是扁平字符串，无租户语义。本模块
是唯一编码实现，禁止各处手拼。Approval 跨进程恢复依赖该编码。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_PATTERN = re.compile(
    r"^tenant:(?P<tenant>[^/]+)"
    r"/user:(?P<user>[^/]+)"
    r"/agent:(?P<agent>[^/]+)"
    r"/session:(?P<session>[^/]+)"
    r"/run:(?P<run>[^/]+)$"
)


class ThreadIdError(ValueError):
    """thread_id 编码不合法或与声明的租户不匹配。"""


class HarnessThreadId(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)

    def encode(self) -> str:
        return (
            f"tenant:{self.tenant_id}/user:{self.user_id}/agent:{self.agent_id}"
            f"/session:{self.session_id}/run:{self.run_id}"
        )

    def tenant_prefix(self) -> str:
        """租户隔离过滤前缀（Checkpointer 读取侧使用）。"""
        return f"tenant:{self.tenant_id}/"


def encode_thread_id(
    *,
    tenant_id: str,
    user_id: str,
    agent_id: str,
    session_id: str,
    run_id: str,
) -> str:
    """构造复合 thread_id。任何字段含 ``/`` 视为注入攻击，拒绝。"""
    for name, value in (
        ("tenant_id", tenant_id),
        ("user_id", user_id),
        ("agent_id", agent_id),
        ("session_id", session_id),
        ("run_id", run_id),
    ):
        if not value or "/" in value:
            raise ThreadIdError(f"{name} 不能为空且不能包含 '/': {value!r}")
    return HarnessThreadId(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
    ).encode()


def decode_thread_id(thread_id: str) -> HarnessThreadId:
    match = _PATTERN.match(thread_id)
    if match is None:
        raise ThreadIdError(
            f"thread_id 不符合租户复合编码 tenant:*/user:*/agent:*/session:*/run:*: {thread_id!r}"
        )
    groups = match.groupdict()
    return HarnessThreadId(
        tenant_id=groups["tenant"],
        user_id=groups["user"],
        agent_id=groups["agent"],
        session_id=groups["session"],
        run_id=groups["run"],
    )


def validate_tenant(thread_id: str, *, tenant_id: str) -> None:
    """写入/读取侧强制校验：thread_id 必须落在声明租户下。"""
    decoded = decode_thread_id(thread_id)
    if decoded.tenant_id != tenant_id:
        raise ThreadIdError(f"thread_id 租户不匹配: {decoded.tenant_id!r} != {tenant_id!r}")


def matches_tenant(thread_id: str, *, tenant_id: str) -> bool:
    """读取侧前缀过滤（宽松版，编码不合法返回 False 而非抛错）。"""
    try:
        validate_tenant(thread_id, tenant_id=tenant_id)
    except ThreadIdError:
        return False
    return True


__all__ = [
    "HarnessThreadId",
    "ThreadIdError",
    "decode_thread_id",
    "encode_thread_id",
    "matches_tenant",
    "validate_tenant",
]
