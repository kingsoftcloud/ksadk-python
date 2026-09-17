# -*- coding: utf-8 -*-
"""Preprod E2E configuration must exercise Gateway authentication safely."""

from __future__ import annotations

import asyncio

from tests.kernel.preprod.conftest import PreprodConfig
from tests.kernel.preprod.test_agent_kernel_preprod_e2e import PreprodClient


def test_preprod_client_keeps_auth_header_process_only():
    config = PreprodConfig(
        server_url="http://server.example.test",
        gateway_url="http://gateway.example.test",
        agent_instance_id="kernel-agent",
        authorization_header="Bearer test-only-value",
    )
    client = PreprodClient(config)
    try:
        assert client._client.headers["authorization"] == "Bearer test-only-value"
        assert "test-only-value" not in repr(config)
    finally:
        asyncio.run(client.aclose())
