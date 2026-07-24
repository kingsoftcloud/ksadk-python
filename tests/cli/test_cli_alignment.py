# -*- coding: utf-8 -*-
"""CLI 对齐 + a2a 注册(Windows bug 回归)+ replay 的测试 (goal-16)。

验收:
- 命令注册失败显式降级(不静默吞,`except ImportError: pass` 残留为零)。
- ``agentengine a2a -h`` 可用(a2a 命令确已注册;ksadk 0.7.0 + a2a-sdk 1.1.x 下不再
  "No such command 'a2a'")。
- ``ksadk replay <session>`` 回放 RuntimeEvent 历史。
"""

from __future__ import annotations

import asyncio

import click
from click.testing import CliRunner

from ksadk.cli import _register_commands, _register_optional_command, cli
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.in_memory import InMemorySessionService

# ---- 注册:显式降级,不静默吞 ----


def test_failed_command_registration_is_explicit_not_silent():
    group = click.Group("t")
    _register_optional_command(group, "ksadk.cli.no_such_module_xyz", "ghost")
    # 失败模块也注册成 stub(显式),不是静默消失
    assert "ghost" in group.commands
    stub = group.commands["ghost"]
    # stub 的 help 显式含失败原因
    assert "不可用" in (stub.help or "")
    # 调用 stub 报显式错误(非静默)
    result = CliRunner().invoke(group, ["ghost"])
    assert "不可用" in result.output


def test_no_try_except_importerror_pass_residue():
    """goal-16 验收:命令注册不再有 ``except ImportError: pass`` 静默吞(限注册路径)。"""
    import inspect

    from ksadk.cli import _register_commands

    src = inspect.getsource(_register_commands)
    assert "except ImportError" not in src, "_register_commands 仍有 ImportError 静默吞残留"


# ---- a2a -h(Windows bug 回归)----


def test_a2a_command_registered_and_help_works():
    _register_commands()
    assert "a2a" in cli.commands, "a2a 命令被静默吞(Windows bug 回归)"
    result = CliRunner().invoke(cli, ["a2a", "-h"])
    assert result.exit_code == 0
    # 新子命令全部可见
    for sub in ("serve", "card", "register", "discover", "call", "status", "cancel"):
        assert sub in result.output


# ---- replay ----


def test_replay_outputs_runtime_event_history(monkeypatch):
    # 用独立 event loop 先存 RuntimeEvent(避免与命令内 asyncio.run 嵌套)。
    async def _seed(svc):
        await svc.create_session(agent_id="a", user_id="u", session_id="s1")
        store = RuntimeEventStore(svc)
        await store.append(
            [
                RuntimeEvent.create(
                    EventType.RUN_STARTED,
                    agent_id="a",
                    user_id="u",
                    session_id="s1",
                    invocation_id="inv1",
                    seq_id=1,
                    payload={"status": "in_progress"},
                ),
                RuntimeEvent.create(
                    EventType.TEXT_DELTA,
                    agent_id="a",
                    user_id="u",
                    session_id="s1",
                    invocation_id="inv1",
                    seq_id=2,
                    payload={"text": "你好"},
                    phase="final_answer",
                ),
            ]
        )

    svc = InMemorySessionService()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_seed(svc))
    finally:
        loop.close()
    # replay 命令经 resolve_session_service 取 session service
    monkeypatch.setattr("ksadk.sessions.resolve_session_service", lambda: svc)

    from ksadk.cli.cmd_replay import replay

    result = CliRunner().invoke(replay, ["s1"])
    assert result.exit_code == 0, result.output
    assert "你好" in result.output
    assert "inv1" in result.output  # run 状态行
