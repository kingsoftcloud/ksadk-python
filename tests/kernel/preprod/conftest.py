# -*- coding: utf-8 -*-
"""Agent Kernel Kernel 预发测试组 conftest（Task 13 Step 1）。

- ``--preprod`` 显式 opt-in：未传时整组预发测试 skip；但
  ``pytest --collect-only`` 仍能看到全部用例。
- 端点 / 测试租户 / 凭据全部来自预发 Secret / env，报告只保存
  resource ref / digest，不保存 DSN / token / prompt 原文。
- gate 逻辑单元测试（fake evidence）与本地 closed-loop 测试不需要
  ``--preprod``，永远执行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

PREPROD_ENV_VARS = (
    "KSADK_PHASE1_SERVER_URL",
    "KSADK_PHASE1_GATEWAY_URL",
    "KSADK_PHASE1_AGENT_INSTANCE_ID",
    "KSADK_PHASE1_AUTHORIZATION_HEADER",
)


def pytest_addoption(parser):
    # 全量 suite 下该 conftest 可能不是 initial conftest；guard 防重复注册。
    try:
        parser.addoption(
            "--preprod",
            action="store_true",
            default=False,
            help="opt in to real preprod E2E tests (tests/kernel)",
        )
    except (ValueError, pytest.OptionError):  # pragma: no cover - defensive
        pass


def _preprod_enabled(pytestconfig) -> bool:
    return bool(pytestconfig.getoption("--preprod", default=False))


class PreprodConfigError(RuntimeError):
    """预发环境变量缺失；缺哪个变量本身也是可报告的诊断信息。"""


@dataclass(frozen=True)
class PreprodConfig:
    """从环境读取的预发目标；不含任何凭据字段。"""

    server_url: str
    gateway_url: str
    agent_instance_id: str
    authorization_header: str

    def __repr__(self) -> str:  # 防止意外打印凭据形态内容
        return (
            f"PreprodConfig(server_url={self.server_url!r}, "
            f"gateway_url={self.gateway_url!r}, "
            f"agent_instance_id={self.agent_instance_id!r}, "
            "authorization_header='<redacted>')"
        )

    @classmethod
    def from_environment(cls) -> "PreprodConfig":
        missing = [name for name in PREPROD_ENV_VARS if not os.environ.get(name)]
        if missing:
            raise PreprodConfigError(
                "missing required preprod env vars: " + ", ".join(sorted(missing))
            )
        return cls(
            server_url=os.environ["KSADK_PHASE1_SERVER_URL"],
            gateway_url=os.environ["KSADK_PHASE1_GATEWAY_URL"],
            agent_instance_id=os.environ["KSADK_PHASE1_AGENT_INSTANCE_ID"],
            authorization_header=os.environ["KSADK_PHASE1_AUTHORIZATION_HEADER"],
        )


@pytest.fixture(scope="session")
def preprod_config(pytestconfig):
    if not _preprod_enabled(pytestconfig):
        pytest.skip("preprod opt-in required")
    return PreprodConfig.from_environment()
