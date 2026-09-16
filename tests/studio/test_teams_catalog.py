from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from ksadk.studio.teams_catalog import StudioTeamsCatalog


def studio():
    draft = NS(metadata=NS(id="engineer", name="工程师"), spec=NS(description="验证实现与测试"))

    def build(key, status="SUCCEEDED"):
        return NS(
            id=key,
            agent_id="engineer",
            status=status,
            artifact_path="artifact",
            runtime_type="harness",
            runtime_lock={},
            bundle_digest="sha256:fixture",
            resolved_digest="",
            created_at=datetime.now(timezone.utc),
        )

    return NS(
        drafts=NS(list=lambda **kw: [draft]),
        builds=NS(list=lambda: [build("new"), build("old"), build("broken", "FAILED")]),
        execution_host=NS(describe=AsyncMock()),
        cloud=NS(gateway=NS()),
    )


@pytest.mark.asyncio
async def test_directory_preserves_versions_and_reasons_without_starting_kernels():
    value = studio()
    items = await StudioTeamsCatalog(value, authority_ref="test").list_bindings()
    assert len(items) == 3
    assert all(item["name"] == "工程师" for item in items)
    assert items[0]["availability"]["state"] == "unchecked"
    assert items[2]["availability"]["code"] == "build_required"
    assert items[2]["capabilities"]["enqueue"] is False
    value.execution_host.describe.assert_not_called()


@pytest.mark.asyncio
async def test_cloud_failure_keeps_local_directory_and_does_not_expose_exception():
    value = studio()
    value.cloud.gateway.list_account_agents = AsyncMock(side_effect=OSError("private-credential"))
    catalog = StudioTeamsCatalog(value, authority_ref="test")
    assert len(await catalog.list_bindings()) == 3
    assert catalog.cloud_error == "cloud_directory_unavailable"


@pytest.mark.asyncio
async def test_chat_only_cloud_agent_is_visible_but_not_executable():
    value = studio()
    value.cloud.gateway.list_account_agents = AsyncMock(
        return_value={
            "items": [
                {"agentId": "cloud", "name": "云端成员", "versionId": "v1", "kernelReady": True}
            ]
        }
    )
    item = (await StudioTeamsCatalog(value, authority_ref="test").list_bindings())[-1]
    assert item["kind"] == "cloud"
    assert item["capabilities"]["enqueue"] is False
    assert item["availability"]["code"] == "server_authority_required"
