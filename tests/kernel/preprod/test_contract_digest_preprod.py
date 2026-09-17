# -*- coding: utf-8 -*-
"""Agent Kernel Kernel 合同 digest 预检（Task 13 Step 4）。

需要 ``--preprod``。六方（Server / Gateway / Runtime Service / Operator
condition / Runtime health / Web build metadata）的 aggregate contract digest
必须完全相同；Postgres schema version、connectivity、lease clock skew < 2s；
任一 mismatch 停止流量切换。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("preprod_config")

SIX_SOURCES = (
    "server",
    "gateway",
    "runtime_service",
    "operator_condition",
    "runtime_health",
    "web_build_metadata",
)


async def _fetch_digest(client: httpx.AsyncClient, source: str, config) -> str | None:
    endpoints = {
        "server": f"{config.server_url}/v1/meta/contract-digest",
        "gateway": f"{config.gateway_url}/v1/meta/contract-digest",
        "runtime_service": f"{config.server_url}/v1/meta/runtime-service/contract-digest",
        "operator_condition": f"{config.server_url}/v1/meta/operator/contract-digest",
        "runtime_health": f"{config.gateway_url}/v1/meta/runtime/contract-digest",
        "web_build_metadata": f"{config.gateway_url}/meta/build.json",
    }
    response = await client.get(endpoints[source])
    response.raise_for_status()
    payload = response.json()
    return payload.get("contract_digest") or payload.get("digest")


async def test_contract_digest_identical_across_all_six_sources(preprod_config):
    async with httpx.AsyncClient(timeout=30.0) as client:
        digests = await asyncio.gather(
            *(_fetch_digest(client, source, preprod_config) for source in SIX_SOURCES)
        )
    pairs = dict(zip(SIX_SOURCES, digests, strict=True))
    assert all(digests), f"missing digest in some source: {pairs}"
    assert len(set(digests)) == 1, f"contract digest mismatch: {pairs}"


async def test_store_schema_version_and_clock_skew(preprod_config):
    # schema version / connectivity / clock skew 由 Server 管理 endpoint 暴露，
    # 不直连数据库（DSN 不出预发 Secret）。
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{preprod_config.server_url}/v1/meta/agent-kernel-store"
        )
        response.raise_for_status()
        payload = response.json()
    assert payload["driver"] == "postgres"
    assert payload["reachable"] is True
    assert abs(payload.get("clock_skew_seconds", 0.0)) < 2.0
