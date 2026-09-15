"""Public A2A surface guards after the wire 1.0 clean rewrite."""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict

import ksadk.a2a as a2a


def test_wire_1_agent_card_uses_supported_interfaces_only():
    card = a2a.build_agent_card(
        name="demo",
        base_url="http://localhost:8081",
        skills=["search"],
    )

    payload = MessageToDict(card)
    assert payload["name"] == "demo"
    assert "url" not in payload
    assert {
        (interface["protocolBinding"], interface["protocolVersion"])
        for interface in payload["supportedInterfaces"]
    } == {("JSONRPC", "1.0"), ("HTTP+JSON", "1.0")}


def test_removed_a2a_03_demo_api_is_not_reexported():
    for legacy_name in (
        "AgentCardBuilder",
        "KsA2AServer",
        "RemoteA2AAgent",
        "RemoteA2AClient",
        "to_a2a",
    ):
        assert not hasattr(a2a, legacy_name)
