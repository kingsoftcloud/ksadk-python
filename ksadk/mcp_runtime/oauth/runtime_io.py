"""Provider HTTP in the Agent, separate from graph state and business tools."""
from dataclasses import replace

import httpx

from .local.protocol import MCPConnection, OAuthProtocol
from .local.transport import PublicOAuthTransport


class RuntimeOAuthExecutor:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def execute(self, operation):
        if operation.get("schema") != "mcp.runtime-io/v1":
            from .client import BrokerError
            raise BrokerError("MCP_RUNTIME_IO_CONTRACT_MISMATCH")
        payload = operation["payload"]
        config = MCPConnection(**payload["config"])
        # Platform callbacks are public HTTPS. Loopback is reserved for explicit
        # local test composition and cannot be enabled by a cloud operation.
        if config.allow_loopback and self.transport is None:
            from .client import BrokerError
            raise BrokerError("OAUTH_UNTRUSTED_ENDPOINT")
        async with httpx.AsyncClient(transport=self.transport or PublicOAuthTransport(),
                timeout=15, follow_redirects=False, trust_env=False) as http:
            protocol = OAuthProtocol(http)
            if operation["kind"] == "prepare":
                resource = await protocol.discover_resource(config, check_authority=False)
                config = protocol.bind_resource(config, resource)
                metadata = await protocol.metadata(config)
                config = replace(config, issuer=metadata["issuer"], redirect_uri=config.registration_redirect_uri())
                client = await protocol.register(config, metadata)
                return {"resource_metadata": resource, "metadata": metadata, "client": client}
            metadata = protocol.validate_metadata(config, payload["metadata"])
            client = protocol.validate_client(config, payload["client"])
            if operation["kind"] in {"exchange", "refresh"}:
                return await protocol.token(config, metadata, client, payload["data"])
            if operation["kind"] == "revoke":
                await protocol.revoke(config, metadata, client, payload["tokens"])
                return {}
            from .client import BrokerError
            raise BrokerError("INVALID_RUNTIME_IO_OPERATION")
