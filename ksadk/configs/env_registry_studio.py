"""Studio startup, local security, and optional Teams authority configuration."""

from ksadk.configs.env_var_spec import EnvVarSpec

STUDIO_ENV_VAR_REGISTRY_ITEMS: tuple[EnvVarSpec, ...] = (
    EnvVarSpec(
        "KSADK_CHANNEL_API_TOKEN",
        "studio",
        "Channel API management token saved in protected Studio configuration.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_CHANNEL_AGENT_",
        "studio",
        "Generated prefix for per-Agent Channel Connector credentials; not a user variable.",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_CHANNEL_WORKSPACE_ID",
        "studio",
        "Default Channel Connector workspace identity.",
    ),
    EnvVarSpec(
        "KSADK_STUDIO_LAZY_START",
        "studio",
        "Internal desktop startup mode; keep the Studio window responsive "
        "while optional DSH warmup runs.",
        "0",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_STUDIO_CHANNEL_DEFAULT",
        "studio",
        "Default-enable the wheel-owned Studio Channel UI bundle after HTTP startup (1=on, 0=off).",
        "1",
    ),
    EnvVarSpec(
        "KSADK_STUDIO_TEAMS_DEFAULT",
        "studio",
        "Default-enable the Teams plugin after HTTP startup (1=on, 0=off).",
        "1",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_STUDIO_NO_SECURITY",
        "studio",
        "Disable Studio loopback session and CSRF checks for controlled tests only.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_STUDIO_AUTHORIZER",
        "studio",
        "Internal authoring backend selector; bounded chat is the default and the "
        "filesystem-capable Codex authorizer requires an explicit opt-in.",
        "chat",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_STUDIO_SESSION_TOKEN",
        "studio",
        "Explicit local Studio browser session token; generated randomly when unset.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_STUDIO_TRACE_CONTENT",
        "studio",
        "Persist Studio trace event content; set to 0 to retain metadata only.",
        "1",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_SERVER_URL", "studio",
        "Explicit Teams authority URL; unset keeps the local workspace authority.",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_ACCESS_TOKEN", "studio",
        "Bearer credential for the configured Teams authority; "
        "unset uses configured request signing.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_TEAMS_NODE_KIND", "studio", "Execution node kind sent during Teams registration.",
        "local",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_NODE_NAME", "studio",
        "Execution node display name sent to the Teams authority.",
        "system hostname",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_PERMIT_ISSUER", "studio",
        "Trusted Teams permit issuer; Studio defaults to agentengine-server; "
        "cloud hosts require it.",
        "agentengine-server (Studio only)",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_RUNTIME_SERVER_URL", "studio",
        "Trusted Teams API base URL supplied to the cloud runtime host.",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_RUNTIME_TARGET", "studio",
        "Frozen CloudTarget JSON supplied by the deployment control plane.",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_RUNTIME_PROVIDER_REF", "studio",
        "Provider identity bound to the cloud runtime host's frozen target.",
    ),
    EnvVarSpec(
        "KSADK_TEAMS_RUNTIME_STATE_DIR", "studio",
        "Per-attempt workspace directory; authoritative execution state remains in PostgreSQL.",
        "/tmp/ksadk-teams",
    ),
)
