from __future__ import annotations

from contextlib import contextmanager

from ksadk.api.client import AgentEngineAPIError
from ksadk.deployment.agent_access import (
    get_latest_agent_access,
    normalize_deployment_status,
)


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.suppression_used = False

    @contextmanager
    def suppress_http_error_logging(self, predicate=None):
        self.suppression_used = predicate is not None
        yield

    async def get_agent(self, *, agent_id=None, name=None, include_api_key=False):
        self.calls += 1
        if self.calls < 3:
            raise AgentEngineAPIError(
                404,
                "未找到对应的 Agent",
                details={
                    "http_status": 404,
                    "remote_error_message": "未找到对应的 Agent",
                },
            )
        return {
            "basic": {
                "agent_id": agent_id or "ar-demo",
                "name": name or "demo-agent",
                "status": "RUNNING",
                "framework": "hermes",
                "region": "pre-online",
            },
            "quick_access": {
                "public_endpoint": "https://agent.example.com",
                "api_key": "ak-demo" if include_api_key else None,
            },
        }


async def _fake_detail_fetcher(agent_ref: str, include_api_key: bool):
    return {
        "agent_id": agent_ref,
        "name": "demo-openclaw",
        "status": "RUNNING",
        "framework": "openclaw",
        "region": "pre-online",
        "endpoint": "https://openclaw.example.com",
        "api_key": "ak-openclaw" if include_api_key else None,
    }


def test_get_latest_agent_access_retries_transient_get_agent_not_found_and_suppresses_logs():
    import asyncio

    client = _FakeClient()

    result = asyncio.run(
        get_latest_agent_access(
            client,
            agent_id="ar-demo",
            attempts=3,
            interval_seconds=0,
            include_api_key=True,
        )
    )

    assert client.suppression_used is True
    assert client.calls == 3
    assert result["agent_id"] == "ar-demo"
    assert result["endpoint"] == "https://agent.example.com"
    assert result["api_key"] == "ak-demo"
    assert result["status"] == "RUNNING"


def test_get_latest_agent_access_supports_custom_detail_fetcher():
    import asyncio

    result = asyncio.run(
        get_latest_agent_access(
            object(),
            agent_id="ar-openclaw-demo",
            attempts=1,
            interval_seconds=0,
            detail_fetcher=_fake_detail_fetcher,
        )
    )

    assert result["agent_id"] == "ar-openclaw-demo"
    assert result["framework"] == "openclaw"
    assert result["region"] == "pre-online"
    assert result["endpoint"] == "https://openclaw.example.com"


def test_normalize_deployment_status_maps_numeric_to_submitted():
    assert normalize_deployment_status(200) == "SUBMITTED"
    assert normalize_deployment_status("running") == "RUNNING"
