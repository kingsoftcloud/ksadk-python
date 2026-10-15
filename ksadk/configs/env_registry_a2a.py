"""A2A environment variable contracts consumed by the canonical registry."""

from __future__ import annotations

from ksadk.configs.env_var_spec import EnvVarSpec

A2A_ENV_VAR_REGISTRY_ITEMS: tuple[EnvVarSpec, ...] = (
    EnvVarSpec(
        "KSADK_A2A_API_KEY",
        "a2a",
        "Product API Key used only by product_key_http_client when api_key is omitted; "
        "never inherits unrelated service credentials.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2A_AGENT_ID",
        "a2a",
        "Managed Runtime target Agent id (ar-*); required for the fail-closed "
        "A2A data-plane composition.",
    ),
    EnvVarSpec(
        "KSADK_A2A_ACCOUNT_ID",
        "a2a",
        "Runtime owner account id injected by the deploy layer (ar-* agent's account).",
    ),
    EnvVarSpec(
        "KSADK_A2A_RUNTIME_ID",
        "a2a",
        "Hosted Agent runtime resource id injected before registration for discovery.",
    ),
    EnvVarSpec(
        "KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED",
        "a2a",
        "Platform-owned marker enabling the complete managed A2A composition.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_A2A_AGENT_NAME",
        "a2a",
        "AgentCard display name injected by the deploy layer; falls back to "
        "AGENTENGINE_MANAGED_RUNTIME_NAME then to the agent id.",
    ),
    EnvVarSpec(
        "KSADK_A2A_AGENT_VERSION",
        "a2a",
        "AgentCard business version injected by the deploy layer; defaults to 0.1.0.",
    ),
    EnvVarSpec(
        "KSADK_A2A_CONTROL_PLANE_URL",
        "a2a",
        "AgentEngine A2A runtime control-plane base URL injected by the deploy layer.",
    ),
    EnvVarSpec(
        "KSADK_A2A_CONTROL_PLANE_CA_FILE",
        "a2a",
        "Platform-projected CA bundle used only for managed A2A control-plane HTTPS.",
    ),
    EnvVarSpec(
        "KSADK_A2A_GATEWAY_CA_FILE",
        "a2a",
        "Optional Gateway CA file added to public trust roots for managed A2A HTTPS.",
    ),
    EnvVarSpec(
        "KSADK_A2A_ENABLE_PUBLIC_EGRESS",
        "a2a",
        "Allow calling external (public-egress) agents in the A2A Space.",
        "false when the deploy layer does not inject a value",
    ),
    EnvVarSpec(
        "KSADK_A2A_EVENT_OUTBOX_PATH",
        "a2a",
        "SQLite path for local-only A2A task-event delivery batches; forbidden in managed Runtime.",
        ".agentengine/a2a_event_outbox.sqlite3",
    ),
    EnvVarSpec(
        "KSADK_A2A_INBOUND_ENABLED",
        "a2a",
        "Enable managed inbound A2A serving; false configures a caller-only Runtime.",
        "true",
    ),
    EnvVarSpec(
        "KSADK_A2A_PUBLIC_TOOL_POLICY_FILE",
        "a2a",
        "Projected JSON file containing the public tool activity disclosure policy.",
    ),
    EnvVarSpec(
        "KSADK_A2A_DATABASE_DSN",
        "a2a",
        "PostgreSQL DSN projected from the AgentKernel store SecretKeyRef.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2A_GATEWAY_PUBLIC_KEY_FILE",
        "a2a",
        "Projected file locator containing the Gateway forward-assertion public key.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2A_AGENT_REGISTRATION_ID",
        "a2a",
        "Opaque registered a2a-agent-* target identity injected by the deploy layer.",
    ),
    EnvVarSpec(
        "KSADK_A2A_INTERNAL_BASE_URL",
        "a2a",
        "Internal HTTP(S) origin used as AgentCard base_url before the gateway rewrites it; "
        "must be an absolute origin with no path/query/fragment.",
    ),
    EnvVarSpec(
        "KSADK_A2A_SPACE_ID",
        "a2a",
        "Primary A2A Space id configured for this Runtime Agent.",
    ),
    EnvVarSpec(
        "KSADK_A2A_SPACE_IDS",
        "a2a",
        "Compatibility JSON array of A2A Space ids configured for this Runtime Agent.",
    ),
    EnvVarSpec(
        "KSADK_A2A_TENANT_ID",
        "a2a",
        "Runtime tenant id injected by the deploy layer; falls back to account id.",
    ),
    EnvVarSpec(
        "KSADK_A2A_TOKEN_DIR",
        "a2a",
        "Directory containing audience-specific projected A2A workload JWT files.",
        "/var/run/secrets/agentengine/a2a",
    ),
    EnvVarSpec(
        "KSADK_A2A_SERVICE_URL",
        "a2a",
        "A2A control plane service URL (KOP public API); auto-detected if unset.",
    ),
    EnvVarSpec(
        "KSADK_A2A_SERVICE_TOKEN",
        "a2a",
        "Bearer token for A2A control plane service authentication.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2A_SERVICE_ENDPOINT",
        "a2a",
        "A2A service endpoint hostname (used for auto-detection with scheme).",
    ),
    EnvVarSpec(
        "KSADK_A2A_SERVICE_SCHEME",
        "a2a",
        "A2A service URL scheme (http/https) for auto-detection.",
    ),
    EnvVarSpec(
        "KSADK_A2A_SERVICE_REGION",
        "a2a",
        "A2A service region for KOP signing.",
    ),
    EnvVarSpec(
        "KSADK_A2A_ACCESS_KEY",
        "a2a",
        "A2A KOP access key for signing; falls back to KSYUN_ACCESS_KEY.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2A_SECRET_KEY",
        "a2a",
        "A2A KOP secret key for signing; falls back to KSYUN_SECRET_KEY.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2A_SERVICE",
        "a2a",
        "A2A KOP signing service name (default: aicp).",
    ),
)
