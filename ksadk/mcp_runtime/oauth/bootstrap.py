"""Independent opt-in extension, assembled only with the durable Kernel."""

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from .client import BrokerClient
from .kernel import MCPAuthorizationExtension


async def configure_runtime(runtime):
    projected = os.getenv("KSADK_MCP_OAUTH_PROJECTION")
    lab_config = None if os.getenv("AGENT_RUNTIME_ID") else os.getenv("KSADK_MCP_OAUTH_CONFIG_FILE")
    if not projected and not lab_config:
        return
    jwks_source = None
    if projected:
        import ssl

        import httpx

        from ksadk.kernel.ingress_bootstrap import _remote_jwks_source

        from .contract import CAPABILITY

        config = json.loads(projected)
        if config.get("digest") != CAPABILITY["digest"]:
            raise RuntimeError("MCP_OAUTH_CONTRACT_MISMATCH")
        jwks_source = _remote_jwks_source()
        public_key = None
        verify = ssl.create_default_context()
        ca = os.getenv("AGENT_CONTROL_JWKS_CA_FILE")
        if ca:
            verify.load_verify_locations(cafile=ca)
        http = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False, verify=verify)
        client = BrokerClient.from_token_file(
            config["broker_url"],
            "/var/run/secrets/agentengine/mcp/agentengine-mcp-broker.jwt",
            http=http,
            project_dir=runtime.config.launch_context.project_dir if runtime.config.launch_context else None,
        )
        config["broker_issuer"] = os.environ["AGENT_CONTROL_PERMIT_ISSUER"]
    else:
        # Explicit local lab compatibility. Hosted deployments strip this env.
        config = json.loads(Path(lab_config).read_text())
        client = BrokerClient.from_token_file(config["broker_url"], config["workload_token_file"])
        public_key = serialization.load_pem_public_key(
            Path(config["broker_public_key_file"]).read_bytes()
        )
    extension = MCPAuthorizationExtension(
        runtime.kernel_store,
        client,
        public_key=public_key,
        issuer=config.get("broker_issuer", "agentengine-mcp-broker"),
        jwks_source=jwks_source,
    )
    try:
        # On restart discover existing waits without contacting the Broker.
        # A fresh runtime creates these tables on its first actual OAuth use.
        async with extension.pool.acquire() as conn:
            extension._schema_ready = bool(
                await conn.fetchval("SELECT to_regclass('mcp_authorization_waits') IS NOT NULL")
            )
    except BaseException:
        await client.close()
        raise
    runtime.worker._mcp_authorization = extension
