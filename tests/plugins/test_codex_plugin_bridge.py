from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from ksadk.plugins.bridges.codex import (
    CodexAppServerPluginBridge,
    CodexBridgeError,
    CodexPluginApprovalRequired,
)


@dataclass
class _Metadata:
    user_agent: str = "Codex Desktop/0.148.0 (test)"


class _FakeTransport:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.installed = False
        self.fail_install_after_mutation = False
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def initialize(self) -> _Metadata:
        return _Metadata()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        response_model: type[BaseModel],
    ) -> BaseModel:
        self.calls.append((method, params))
        if method == "marketplace/add":
            payload: dict[str, Any] = {
                "marketplaceName": "fixture-market",
                "installedRoot": "/tmp/fixture-market",
                "alreadyAdded": False,
            }
        elif method == "plugin/list":
            payload = self._list_payload()
        elif method == "plugin/read":
            payload = {
                "plugin": {
                    "marketplaceName": "fixture-market",
                    "marketplacePath": "/tmp/fixture-market/.agents/plugins/marketplace.json",
                    "summary": self._summary(),
                    "description": "Fixture plugin",
                    "shareUrl": None,
                    "skills": [
                        {
                            "name": "fixture:review",
                            "description": "Review files",
                            "shortDescription": None,
                            "interface": None,
                            "path": "/tmp/fixture/SKILL.md",
                            "enabled": True,
                        }
                    ],
                    "hooks": [{"eventName": "sessionStart", "key": "fixture-hook"}],
                    "apps": [{"id": "fixture-app", "name": "Fixture App"}],
                    "appTemplates": [],
                    "mcpServers": ["fixture-mcp"],
                    "scheduledTasks": [
                        {
                            "key": "daily",
                            "name": "Daily",
                            "prompt": "run",
                            "schedule": {"rrule": "FREQ=DAILY"},
                        }
                    ],
                }
            }
        elif method == "plugin/install":
            self.installed = True
            if self.fail_install_after_mutation:
                raise RuntimeError("install receipt was lost")
            payload = {"authPolicy": "ON_INSTALL", "appsNeedingAuth": []}
        elif method == "plugin/uninstall":
            self.installed = False
            payload = {}
        else:  # proves the bridge never needs a generic method
            raise AssertionError(f"unexpected RPC {method}")
        return response_model.model_validate(payload)

    def _summary(self) -> dict[str, Any]:
        return {
            "id": "fixture-plugin@fixture-market",
            "remotePluginId": None,
            "version": "9.9.9",
            "localVersion": "1.2.3",
            "name": "fixture-plugin",
            "shareContext": None,
            "source": {"type": "local", "path": "/tmp/fixture-plugin"},
            "installed": self.installed,
            "installedAt": None,
            "enabled": self.installed,
            "installPolicy": "AVAILABLE",
            "installPolicySource": None,
            "mustShowInstallationInterstitial": None,
            "authPolicy": "ON_INSTALL",
            "availability": "AVAILABLE",
            "disabledReason": None,
            "eligiblePlanTypes": None,
            "interface": None,
            "keywords": [],
        }

    def _list_payload(self) -> dict[str, Any]:
        return {
            "marketplaces": [
                {
                    "name": "fixture-market",
                    "path": "/tmp/fixture-market/.agents/plugins/marketplace.json",
                    "interface": {"displayName": "Fixture Market"},
                    "plugins": [self._summary()],
                }
            ],
            "marketplaceLoadErrors": [],
            "featuredPluginIds": [],
        }


class _UnreportedVersionTransport(_FakeTransport):
    async def initialize(self) -> dict[str, str]:
        # The App Server's lifecycle RPC is authoritative; userAgent is only
        # display metadata and may be absent on a newer host implementation.
        return {"userAgent": "codex-python-sdk"}


@pytest.mark.asyncio
async def test_codex_bridge_lists_reads_and_prefers_local_version() -> None:
    transport = _FakeTransport()
    async with CodexAppServerPluginBridge(transport=transport) as bridge:
        assert bridge.host.version == "0.148.0"
        assert await bridge.add_marketplace("/tmp/fixture-market") == "fixture-market"
        inventory = await bridge.list_plugins()
        assert len(inventory) == 1
        assert inventory[0].version == "1.2.3"
        assert inventory[0].permissions_declared is False
        assert "host user" in " ".join(inventory[0].risk_disclosures)

        detail = await bridge.read_plugin("fixture-plugin")
        assert detail.skills == ("fixture:review",)
        assert detail.mcp_servers == ("fixture-mcp",)
        assert detail.apps == ("fixture-app",)
        assert detail.scheduled_tasks == ("daily",)

    assert transport.closed is True


@pytest.mark.asyncio
async def test_codex_bridge_keeps_lifecycle_available_when_host_version_is_unreported() -> None:
    transport = _UnreportedVersionTransport()
    async with CodexAppServerPluginBridge(transport=transport) as bridge:
        assert bridge.host.version == "unreported"
        assert await bridge.add_marketplace("/tmp/fixture-market") == "fixture-market"
    assert transport.closed is True


@pytest.mark.asyncio
async def test_codex_install_requires_explicit_undeclared_permission_acceptance() -> None:
    transport = _FakeTransport()
    async with CodexAppServerPluginBridge(transport=transport) as bridge:
        with pytest.raises(CodexPluginApprovalRequired, match="explicit"):
            await bridge.install_plugin("fixture-plugin")
        assert all(method != "plugin/install" for method, _ in transport.calls)

        result = await bridge.install_plugin(
            "fixture-plugin",
            accept_undeclared_permissions=True,
            install_attempt_id="fixture-attempt",
        )
        assert result.inventory.installed is True
        assert result.inventory.enabled is True
        assert result.inventory.version == "1.2.3"

        removed = await bridge.uninstall_plugin(result.inventory.plugin_id)
        assert removed.plugin_id == result.inventory.plugin_id
        assert removed.installed is False
        assert removed.enabled is False


@pytest.mark.asyncio
async def test_failed_install_compensates_mutation_and_preserves_old_inventory() -> None:
    transport = _FakeTransport()
    transport.fail_install_after_mutation = True
    async with CodexAppServerPluginBridge(transport=transport) as bridge:
        before = (await bridge.list_plugins(force_refetch=True))[0]

        with pytest.raises(CodexBridgeError, match="previous inventory was restored"):
            await bridge.install_plugin(
                "fixture-plugin",
                accept_undeclared_permissions=True,
                install_attempt_id="lost-receipt",
            )

        after = (await bridge.list_plugins(force_refetch=True))[0]
        assert after == before
        assert transport.installed is False
        assert [method for method, _params in transport.calls].count("plugin/uninstall") == 1


@pytest.mark.asyncio
async def test_bridge_has_no_arbitrary_rpc_surface_and_rejects_unstarted_use() -> None:
    bridge = CodexAppServerPluginBridge(transport=_FakeTransport())
    assert not hasattr(bridge, "request")
    with pytest.raises(CodexBridgeError, match="not started"):
        await bridge.list_plugins()


@pytest.mark.asyncio
async def test_injected_transport_cannot_be_combined_with_launch_options(tmp_path) -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        CodexAppServerPluginBridge(transport=_FakeTransport(), codex_home=tmp_path)


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
