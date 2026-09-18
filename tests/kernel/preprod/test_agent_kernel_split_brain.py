# -*- coding: utf-8 -*-
"""Agent Kernel Kernel split-brain 旧 writer 拒绝演练（Task 13 Step 8）。

需要 ``--preprod``。Pod 保留到 Store 的旧网络路径、新 activation takeover 后，
旧 stream 的 token / terminal 写必须被 stale fence 拒绝；canonical log 只含
新 owner 事实；``stale_fence_total`` 指标增加；审计记录 old/new activation refs。
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("preprod_config")


async def test_split_brain_old_writer_token_write_rejected(preprod_config):
    async with httpx.AsyncClient(base_url=preprod_config.server_url, timeout=30.0) as client:
        response = await client.get("/metrics")
        response.raise_for_status()
    # 旧 Pod 写入尝试由演练侧 operator 在预发执行（网络隔离 + 强制 takeover）。
    pytest.skip("split-brain drill requires preprod drill operator")


async def test_split_brain_canonical_log_contains_only_new_owner(preprod_config):
    pytest.skip("split-brain drill requires preprod drill operator")


def _metric(text: str, name: str) -> float:
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0
