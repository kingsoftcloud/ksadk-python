"""Channel Connector — outbound WSS client for local Agent ↔ cloud Channel.

Usage::

    from ksadk.connector import start_channel_connector

    stop = start_channel_connector(
        url="wss://channel.example.com/agentengine/connector/v1",
        token="eyJ...",
        agent_id="my-agent",
        tenant_id="tenant-abc",
        workspace_id="ws-001",
        invoke=my_invoke_fn,   # async (task_id, message) -> str
    )
    ...
    stop()
"""
from ksadk.connector.channel import start_channel_connector, ChannelConnector

__all__ = ["start_channel_connector", "ChannelConnector"]
