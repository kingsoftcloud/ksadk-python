from __future__ import annotations

import asyncio
import json
import time

import pytest

from ksadk.plugins.companion_artifacts import DshCompanionArtifact
from ksadk.plugins.companions import (
    CompanionError,
    DshCompanionDefinition,
    DshPluginCompanionManager,
)


async def _exchange(path, request, *, keep=False):
    reader, writer = await asyncio.open_unix_connection(path)
    raw = json.dumps({"v": 1, "requestId": "test", **request}).encode()
    writer.write(len(raw).to_bytes(4, "big") + raw)
    await writer.drain()
    size = int.from_bytes(await reader.readexactly(4), "big")
    result = json.loads(await reader.readexactly(size))
    if not keep:
        writer.close()
        await writer.wait_closed()
    return result, writer


@pytest.mark.asyncio
async def test_graph_health_opaque_principal_and_revoke_before_close():
    events = []
    principal = object()

    async def invoke(actor, operation, arguments, call_id):
        await asyncio.sleep(0)
        assert actor is principal
        events.append("invoke")
        return {"operation": operation, "callId": call_id}

    definition = DshCompanionDefinition(
        plugin_id="example.plugin",
        components={"one": "pkg-one", "two": "pkg-two"},
        operations=frozenset({"read"}),
        start=lambda: events.append("start"),
        revoke=lambda: events.append("revoke"),
        close=lambda: events.append("close"),
        invoke=invoke,
    )
    manager = DshPluginCompanionManager(
        profile="web",
        generation_id="generation-test",
        definitions=(definition,),
    )
    path = await manager.start_broker()
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    config = manager.configuration

    async def register(component, secret=config["secret"]):
        return await _exchange(
            path,
            {
                "type": "register",
                "pluginId": "example.plugin",
                "component": component,
                "generationId": manager.generation_id,
                "secret": secret,
            },
            keep=True,
        )

    try:
        rejected, invalid = await register("one", "forged")
        assert rejected["error"]["code"] == "COMPANION_REGISTRATION_INVALID"
        invalid.close()
        first, one = await register("one")
        assert first["result"]["registered"]
        with pytest.raises(CompanionError, match="COMPANION_GRAPH_NOT_READY"):
            await manager.confirm_core_ready()
        assert events == []
        _, two = await register("two")
        assert events == []
        await manager.confirm_core_ready()
        assert events == ["start"]
        handle = manager.issue_invocation(
            "example.plugin",
            principal,
            operations=frozenset({"read"}),
        )
        base = {
            "type": "invoke",
            "pluginId": "example.plugin",
            "operation": "read",
            "callId": "native-call",
            "arguments": {},
            "deadline": int(time.time() * 1000) + 5000,
        }
        denied, _ = await _exchange(path, {**base, "handle": "forged", "principal": "owner"})
        assert denied["error"]["code"] == "COMPANION_INVOCATION_DENIED"
        result, _ = await _exchange(path, {**base, "handle": handle, "principal": "forged"})
        assert result["result"] == {"operation": "read", "callId": "native-call"}
        one.close()
        await one.wait_closed()
        for _ in range(100):
            if events[-2:] == ["revoke", "close"]:
                break
            await asyncio.sleep(0.01)
        assert events == ["start", "invoke", "revoke", "close"]
        assert not manager.active_plugins
        denied, _ = await _exchange(path, {**base, "handle": handle})
        assert denied["error"]["code"] == "COMPANION_INVOCATION_DENIED"
        _, replacement = await register("one")
        assert events[-1] == "start"
        assert manager.active_plugins == frozenset({"example.plugin"})
        replacement.close()
        two.close()
    finally:
        await manager.close()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["bind", "start"])
async def test_failed_companion_start_closes_resources_and_cannot_issue_handles(failure_phase):
    events = []

    def fail(*args):
        raise RuntimeError("start failed")

    definition = DshCompanionDefinition(
        plugin_id="example.plugin",
        components={"one": "pkg"},
        operations=frozenset({"read"}),
        start=fail if failure_phase == "start" else lambda: events.append("start"),
        revoke=lambda: events.append("revoke"),
        close=lambda: events.append("close"),
        invoke=lambda *args: {},
        bind_artifact=fail if failure_phase == "bind" else None,
    )
    manager = DshPluginCompanionManager(
        profile="web", generation_id="test", definitions=(definition,),
        artifacts={"example.plugin": DshCompanionArtifact(
            plugin_id="example.plugin", profile="web", components=(),
            dependency_lock_digest="fixture-lock", installation_digest="fixture-installed",
            source_digest="fixture-source", profile_digest="fixture-profile",
            host_version="0.1.5-rc.1",
        )},
    )
    path = await manager.start_broker()
    config = manager.configuration
    try:
        _, writer = await _exchange(
            path,
            {
                "type": "register",
                "pluginId": "example.plugin",
                "component": "one",
                "generationId": "test",
                "secret": config["secret"],
            },
            keep=True,
        )
        with pytest.raises(RuntimeError, match="start failed"):
            await manager.confirm_core_ready()
        assert events == ["revoke", "close"]
        with pytest.raises(CompanionError, match="COMPANION_UNAVAILABLE"):
            manager.issue_invocation("example.plugin", object(), operations=frozenset({"read"}))
        writer.close()
    finally:
        await manager.close()
