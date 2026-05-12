from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    module: str
    purpose: str
    default: str = ""
    sensitive: bool = False


ENV_VAR_REGISTRY: tuple[EnvVarSpec, ...] = (
    EnvVarSpec("KSADK_ADK_SESSION_BACKEND", "sessions", "ADK-native session backend selector."),
    EnvVarSpec("KSADK_ADK_SESSION_PATH", "sessions", "ADK-native SQLite session database path."),
    EnvVarSpec("KSADK_ADK_SESSION_URL", "sessions", "ADK-native database session URL.", sensitive=True),
    EnvVarSpec("KSADK_ALLOWED_SUFFIXES", "builders", "Internal code package allowed suffix constant."),
    EnvVarSpec(
        "KSADK_ATTACHMENT_RUNTIME_REQUIREMENTS",
        "builders",
        "Internal bundled attachment requirement constant.",
    ),
    EnvVarSpec(
        "KSADK_CORE_RUNTIME_REQUIREMENTS",
        "builders",
        "Internal bundled core requirement constant.",
    ),
    EnvVarSpec("KSADK_ENABLE_MCP_TOOLS", "mcp_runtime", "Enable ADK MCP tool injection.", "1"),
    EnvVarSpec("KSADK_ENABLE_SANDBOX_TOOLS", "sandbox", "Enable default ADK sandbox tools.", "1"),
    EnvVarSpec("KSADK_EVENTS_TABLE", "sessions", "Internal SQLite events table constant."),
    EnvVarSpec("KSADK_FEISHU_APP_ID", "cli", "Feishu helper app id used by OpenClaw diagnostics."),
    EnvVarSpec("KSADK_FEISHU_RESULT_PATH", "cli", "Feishu helper result file path."),
    EnvVarSpec("KSADK_KB", "knowledge_base", "AICP knowledge-base connection prefix."),
    EnvVarSpec("KSADK_KB_ACCESS_KEY", "knowledge_base", "Knowledge-base API access key.", sensitive=True),
    EnvVarSpec("KSADK_KB_DATASET_ID", "knowledge_base", "Knowledge-base dataset id."),
    EnvVarSpec("KSADK_KB_ENDPOINT", "knowledge_base", "Knowledge-base API endpoint.", "aicp.api.ksyun.com"),
    EnvVarSpec("KSADK_KB_REGION", "knowledge_base", "Knowledge-base region.", "cn-beijing-6"),
    EnvVarSpec("KSADK_KB_RERANKING_ENABLE", "knowledge_base", "Enable knowledge-base reranking.", "false"),
    EnvVarSpec("KSADK_KB_SCORE_THRESHOLD", "knowledge_base", "Knowledge-base score threshold.", "0.0"),
    EnvVarSpec("KSADK_KB_SEARCH_METHOD", "knowledge_base", "Knowledge-base search method.", "intelligence_search"),
    EnvVarSpec("KSADK_KB_SECRET_KEY", "knowledge_base", "Knowledge-base API secret key.", sensitive=True),
    EnvVarSpec("KSADK_KB_TOP_K", "knowledge_base", "Knowledge-base retrieval result count.", "5"),
    EnvVarSpec("KSADK_LTM", "memory", "AICP long-term-memory connection prefix."),
    EnvVarSpec("KSADK_LTM_ACCESS_KEY", "memory", "Long-term-memory API access key.", sensitive=True),
    EnvVarSpec("KSADK_LTM_AGENT_ID", "memory", "Long-term-memory agent id."),
    EnvVarSpec("KSADK_LTM_APP_NAME", "memory", "Long-term-memory application name override."),
    EnvVarSpec("KSADK_LTM_BACKEND", "memory", "Long-term-memory backend selector.", "local"),
    EnvVarSpec("KSADK_LTM_ENDPOINT", "memory", "Long-term-memory API endpoint."),
    EnvVarSpec("KSADK_LTM_HTTP_TOKEN", "memory", "HTTP long-term-memory bearer token.", sensitive=True),
    EnvVarSpec("KSADK_LTM_HTTP_URL", "memory", "HTTP long-term-memory service URL.", sensitive=True),
    EnvVarSpec("KSADK_LTM_INDEX", "memory", "Long-term-memory index name."),
    EnvVarSpec("KSADK_LTM_NAMESPACE", "memory", "Long-term-memory namespace."),
    EnvVarSpec("KSADK_LTM_REGION", "memory", "Long-term-memory region.", "cn-beijing-6"),
    EnvVarSpec("KSADK_LTM_SCENE_ID", "memory", "Long-term-memory scene id."),
    EnvVarSpec("KSADK_LTM_SCHEME", "memory", "Long-term-memory API scheme.", "https"),
    EnvVarSpec("KSADK_LTM_SECRET_KEY", "memory", "Long-term-memory API secret key.", sensitive=True),
    EnvVarSpec("KSADK_LTM_TOP_K", "memory", "Long-term-memory retrieval result count.", "5"),
    EnvVarSpec("KSADK_MCP_SERVERS", "mcp_runtime", "JSON array of MCP server configs.", sensitive=True),
    EnvVarSpec("KSADK_MEMORY_BACKEND", "memory", "Generic memory backend selector.", "memory"),
    EnvVarSpec("KSADK_MEMORY_PREFIX", "memory", "Generic memory key prefix.", "ksadk:memory:"),
    EnvVarSpec("KSADK_MEMORY_TTL", "memory", "Generic memory default TTL seconds."),
    EnvVarSpec("KSADK_MEMORY_URL", "memory", "Generic memory backend URL.", sensitive=True),
    EnvVarSpec("KSADK_PG_EVENTS_TABLE", "sessions", "Internal PostgreSQL events table constant."),
    EnvVarSpec("KSADK_PG_SESSIONS_TABLE", "sessions", "Internal PostgreSQL sessions table constant."),
    EnvVarSpec("KSADK_PG_STATES_TABLE", "sessions", "Internal PostgreSQL states table constant."),
    EnvVarSpec("KSADK_PROJECT_DIR", "sessions", "Project root used for local session/workspace state."),
    EnvVarSpec("KSADK_RESPONSES_SESSION_HEADER", "runners", "Header name for remote Responses session propagation."),
    EnvVarSpec("KSADK_RUNTIME_PORT", "cli", "Runtime HTTP port exported to template runtimes.", "8080"),
    EnvVarSpec("KSADK_RUNTIME_REQUIREMENTS", "builders", "Internal bundled runtime requirements constant."),
    EnvVarSpec("KSADK_SESSIONS_TABLE", "sessions", "Internal SQLite sessions table constant."),
    EnvVarSpec("KSADK_SESSION_BACKEND", "sessions", "Conversation session backend selector.", "local"),
    EnvVarSpec("KSADK_SESSION_DSN", "sessions", "Conversation session database DSN.", sensitive=True),
    EnvVarSpec("KSADK_SESSION_NAMESPACE", "sessions", "Conversation session namespace."),
    EnvVarSpec("KSADK_SESSION_PATH", "sessions", "Conversation local SQLite database path."),
    EnvVarSpec("KSADK_STATES_TABLE", "sessions", "Internal SQLite states table constant."),
    EnvVarSpec("KSADK_STM_BACKEND", "sessions", "Short-term-memory session backend selector."),
    EnvVarSpec("KSADK_STM_DB_PATH", "sessions", "Legacy short-term-memory SQLite path."),
    EnvVarSpec("KSADK_STM_DB_URL", "sessions", "Legacy short-term-memory database URL.", sensitive=True),
    EnvVarSpec("KSADK_STM_PATH", "sessions", "Short-term-memory SQLite path."),
    EnvVarSpec("KSADK_STM_URL", "sessions", "Short-term-memory database URL.", sensitive=True),
    EnvVarSpec("KSADK_TENANT_ID", "sessions", "Tenant id used for session namespace scoping."),
    EnvVarSpec("KSADK_UPDATED_AT", "configs", "Internal config update timestamp field."),
    EnvVarSpec("KSADK_VERSION", "configs", "Internal config version field."),
    EnvVarSpec("KSADK_WORKSPACE_ID", "sessions", "Workspace id used for session namespace scoping."),
)


def iter_env_vars() -> tuple[EnvVarSpec, ...]:
    return ENV_VAR_REGISTRY
