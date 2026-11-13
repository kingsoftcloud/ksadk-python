from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.tunnel.registry import ServerDescriptor, TunnelRegistry


def connection(connector_id: str, agent: str, names: list[str]):
    return SimpleNamespace(
        connector_id=connector_id,
        agent_name=agent,
        servers=[ServerDescriptor(name=name) for name in names],
    )


def test_registry_instances_and_agent_routes_are_isolated() -> None:
    first = TunnelRegistry()
    second = TunnelRegistry()
    assert first.proxy_token != second.proxy_token

    first.add_connection(connection("one", "agent-a", ["docs", "search"]))
    first.add_connection(connection("two", "agent-b", ["docs"]))

    assert [item.name for item in first.list_servers("agent-a")] == ["docs", "search"]
    assert [item.name for item in first.list_servers("agent-b")] == ["docs"]
    assert second.list_servers("agent-a") == []
    assert first.find_connection("agent-a", "docs").connector_id == "one"
    assert first.find_connection("agent-b", "docs").connector_id == "two"
    assert first.find_connection("agent-b", "search") is None


def test_conflicts_are_atomic_and_invalid_registrations_write_nothing() -> None:
    registry = TunnelRegistry()
    registry.add_connection(connection("old", "agent", ["docs"]))
    conflicting = connection("new", "agent", ["fresh", "docs"])

    with pytest.raises(ValueError, match="already registered"):
        registry.add_connection(conflicting)

    assert registry.find_connection("agent", "fresh") is None
    assert registry.find_connection("agent", "docs").connector_id == "old"
    assert [item.name for item in registry.list_servers("agent")] == ["docs"]

    invalid = [
        connection("empty", "agent", []),
        connection("duplicate", "agent", ["same", "same"]),
        SimpleNamespace(
            connector_id="protocol",
            agent_name="agent",
            servers=[ServerDescriptor(name="other", protocol="mcp")],
        ),
    ]
    invalid[2].servers[0] = ServerDescriptor.model_construct(name="other", protocol="stdio")
    invalid.append(connection("blank-agent", " ", ["ok"]))
    for candidate in invalid:
        with pytest.raises(ValueError):
            registry.add_connection(candidate)
    assert len(registry.list_servers("agent")) == 1


def test_stale_connection_cleanup_cannot_remove_replacement() -> None:
    registry = TunnelRegistry()
    old = connection("stable-id", "agent", ["docs"])
    registry.add_connection(old)
    registry.remove_connection(old.connector_id, old)

    replacement = connection("stable-id", "agent", ["docs"])
    registry.add_connection(replacement)
    registry.remove_connection(old.connector_id, old)

    assert registry.find_connection("agent", "docs") is replacement
    registry.remove_connection(replacement.connector_id, replacement)
    assert registry.find_connection("agent", "docs") is None


def test_descriptor_rejects_local_secret_fields() -> None:
    with pytest.raises(ValueError):
        ServerDescriptor(
            name="docs",
            address="http://local.example/mcp",
            headers={"Authorization": "secret"},
        )
