from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from ksadk.plugins.bridges.codex import CodexAppServerPluginBridge


def _summary() -> dict[str, Any]:
    return {
        "id": "ksadk-test@fixture-marketplace",
        "name": "ksadk-test",
        "source": {
            "type": "local",
            "path": "/materialized/ksadk-test",
            "futureSourceField": "preserved-by-wire-model",
        },
        "installed": True,
        "enabled": True,
        "installPolicy": "AVAILABLE",
        "authPolicy": "ON_INSTALL",
        "version": "1.0.0",
        "futureSummaryField": {"additive": True},
    }


class _ForwardCompatibleTransport:
    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def initialize(self) -> dict[str, str]:
        return {"userAgent": "Codex/9.8.7", "futureInitializeField": "ignored"}

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        response_model: type[BaseModel],
    ) -> BaseModel:
        del params
        if method == "marketplace/add":
            payload: dict[str, Any] = {
                "marketplaceName": "fixture-marketplace",
                "installedRoot": "/materialized/marketplace",
                "alreadyAdded": False,
                "futureReceiptField": 1,
            }
        elif method == "plugin/list":
            payload = {
                "marketplaces": [
                    {
                        "name": "fixture-marketplace",
                        "path": "/source/marketplace.json",
                        "plugins": [_summary()],
                        "futureMarketplaceField": "newer-host",
                    }
                ],
                "futureListField": [],
            }
        elif method == "plugin/read":
            payload = {
                "plugin": {
                    "marketplaceName": "fixture-marketplace",
                    "marketplacePath": "/source/marketplace.json",
                    "summary": _summary(),
                    "description": "Fixture",
                    "shareUrl": "codex://plugins/ksadk-test",
                    "skills": [
                        {
                            "name": "ksadk-test:review",
                            "path": "/materialized/ksadk-test/skills/review/SKILL.md",
                            "futureSkillField": {"preserved": True},
                        }
                    ],
                    "hooks": [
                        {
                            "key": "SessionStart",
                            "manifestPath": "/materialized/ksadk-test/hooks.json",
                        }
                    ],
                    "apps": [{"id": "connector_fixture", "required": True}],
                    "appTemplates": [{"id": "template_fixture", "kind": "new"}],
                    "mcpServers": ["counter"],
                    "scheduledTasks": [{"key": "daily-check", "cron": "0 0 * * *"}],
                    "futurePluginField": {"host": "newer"},
                },
                "futureReadField": True,
            }
        else:  # pragma: no cover - the bridge allowlist bounds this fixture
            raise AssertionError(f"unexpected method: {method}")
        return response_model.model_validate(payload)


@pytest.mark.asyncio
async def test_bridge_tolerates_additive_host_fields_and_retains_component_metadata() -> None:
    transport = _ForwardCompatibleTransport()

    async with CodexAppServerPluginBridge(transport=transport) as bridge:
        assert bridge.host.version == "9.8.7"
        receipt = await bridge.add_marketplace_with_receipt("/source")
        assert receipt.marketplace_name == "fixture-marketplace"
        assert receipt.installed_root == "/materialized/marketplace"
        assert receipt.already_added is False

        detail = await bridge.read_plugin(
            "ksadk-test",
            marketplace_name="fixture-marketplace",
        )

    assert transport.closed is True
    assert detail.share_url == "codex://plugins/ksadk-test"
    assert detail.skills == ("ksadk-test:review",)
    assert detail.mcp_servers == ("counter",)
    assert detail.hooks == ("SessionStart",)
    assert detail.apps == ("connector_fixture",)
    assert detail.scheduled_tasks == ("daily-check",)
    assert detail.host_metadata == {"futurePluginField": {"host": "newer"}}

    components = {(component.kind, component.name): component for component in detail.components}
    skill = components[("skill", "ksadk-test:review")]
    assert skill.path == "/materialized/ksadk-test/skills/review/SKILL.md"
    assert skill.metadata["futureSkillField"] == {"preserved": True}
    hook = components[("hook", "SessionStart")]
    assert hook.path == "/materialized/ksadk-test/hooks.json"
    assert components[("app-template", "template_fixture")].metadata["kind"] == "new"
    assert components[("scheduled-task", "daily-check")].metadata["cron"] == "0 0 * * *"
