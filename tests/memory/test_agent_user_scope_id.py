"""Cloud identity encoding follows the upstream AICP format contract.

This encoder is applied only at the cloud boundary. Local logical namespaces
remain compatible with existing records; persistence is covered separately.
"""

from __future__ import annotations

import hashlib

import pytest

from ksadk.memory.scope_ids import (
    SAFE_SCOPE_ID,
    SCOPE_ID_DIGEST_CHARS,
    SCOPE_ID_MAX_READABLE,
    SCOPE_ID_SEPARATOR,
    aicp_agent_user_scope_id as agent_user_scope_id,
)

AICP_LIMIT = 64


def _digest(agent: str, user: str) -> str:
    return hashlib.sha256(f"agent-user-scope/v1|{agent}|{user}".encode()).hexdigest()[
        :SCOPE_ID_DIGEST_CHARS
    ]


@pytest.mark.parametrize(
    ("agent_id", "user_id"),
    [
        ("food-agent", "local-user"),
        ("ar-example-agent-0000000001", "fixture-user-00000000"),
        ("agent", "中文用户"),
        ("agent", "user@example.com"),
        ("agent", "arn:aws:iam::123456789012:user/foo"),
        ("a" * 200, "b" * 200),
        ("", "user@example.com"),
        ("", "user name with spaces"),
        ("a_b", "c"),
        ("a", "b_c"),
    ],
)
def test_scope_id_is_aicp_safe(agent_id: str, user_id: str) -> None:
    scope_id = agent_user_scope_id(agent_id=agent_id, user_id=user_id)
    assert SAFE_SCOPE_ID.fullmatch(scope_id), scope_id
    assert len(scope_id) <= AICP_LIMIT, scope_id


def test_readable_form_is_used_when_it_fits() -> None:
    """调查一条记忆属于哪个分区时，可读 id 比摘要有用得多。"""
    assert agent_user_scope_id(agent_id="food-agent", user_id="local-user") == "food-agent_local-user"
    # 示例 AgentId × 用户；组合长度仍在余量之内。
    combined = agent_user_scope_id(
        agent_id="ar-example-agent-0000000001", user_id="fixture-user-00000000"
    )
    assert combined == "ar-example-agent-0000000001_fixture-user-00000000"
    assert len(combined) < SCOPE_ID_MAX_READABLE < AICP_LIMIT


def test_overlong_pair_falls_back_to_a_short_digest() -> None:
    agent, user = "a" * 40, "b" * 40
    scope_id = agent_user_scope_id(agent_id=agent, user_id=user)
    assert scope_id == _digest(agent, user)
    assert len(scope_id) == SCOPE_ID_DIGEST_CHARS == 32
    # 旧的 60 字符摘要顶在 64 上限上；新长度留出一倍余量。
    assert len(scope_id) * 2 <= AICP_LIMIT


@pytest.mark.parametrize(("agent_id", "user_id"), [("a_b", "c"), ("a", "b_c")])
def test_separator_in_either_part_forces_a_digest(agent_id: str, user_id: str) -> None:
    """两侧都允许下划线，直接拼接会让 ``a_b``+``c`` 与 ``a``+``b_c`` 撞成同一命名空间。"""
    assert agent_user_scope_id(agent_id=agent_id, user_id=user_id) == _digest(agent_id, user_id)


def test_separator_collision_is_impossible() -> None:
    one = agent_user_scope_id(agent_id="a_b", user_id="c")
    two = agent_user_scope_id(agent_id="a", user_id="b_c")
    assert one != two


def test_scope_id_hashes_agent_and_user_together() -> None:
    """同一用户在不同 Agent 下不能共享命名空间，否则跨 Agent 会串记忆。"""
    one = agent_user_scope_id(agent_id="agent-a", user_id="same-user")
    two = agent_user_scope_id(agent_id="agent-b", user_id="same-user")
    assert one != two
    assert one.startswith(f"agent-a{SCOPE_ID_SEPARATOR}")


def test_scope_id_is_stable_and_trims_input() -> None:
    first = agent_user_scope_id(agent_id="agent-a", user_id="user-1")
    second = agent_user_scope_id(agent_id=" agent-a ", user_id=" user-1 ")
    assert first == second == f"agent-a{SCOPE_ID_SEPARATOR}user-1"


def test_scope_id_keeps_an_already_safe_user_namespace() -> None:
    """无 Agent 时保留既有的本地命名空间，历史记忆不能因为改 ID 而失联。"""
    assert agent_user_scope_id(agent_id="", user_id="local-user") == "local-user"
    assert agent_user_scope_id(agent_id=None, user_id="local_user") == "local_user"


def test_scope_id_never_contains_a_colon() -> None:
    """回归点：冒号是这次线上故障的直接原因。"""
    for agent_id, user_id in [
        ("food-agent", "local-user"),
        ("", "local-user"),
        ("agent", "中文用户"),
    ]:
        assert ":" not in agent_user_scope_id(agent_id=agent_id, user_id=user_id)
