from __future__ import annotations

from ksadk.configs.env_registry_pcm import PCM_ENV_VAR_REGISTRY_ITEMS
from ksadk.configs.env_var_spec import EnvVarSpec

_ENV_VAR_REGISTRY_ITEMS: tuple[EnvVarSpec, ...] = (
    EnvVarSpec(
        "KSADK_AGENT_EVAL",
        "evaluation",
        "Enable internal Agent evaluation integration.",
        "0",
        documented=False,
    ),
    EnvVarSpec("KSADK_ADK_RESUMABLE", "runners", "Enable ADK invocation resume support.", "false"),
    EnvVarSpec("KSADK_ADK_SESSION_BACKEND", "sessions", "ADK-native session backend selector."),
    EnvVarSpec("KSADK_ADK_SESSION_PATH", "sessions", "ADK-native SQLite session database path."),
    EnvVarSpec(
        "KSADK_ADK_SESSION_URL", "sessions", "ADK-native database session URL.", sensitive=True
    ),
    EnvVarSpec(
        "KSADK_A2A_AGENT_ID",
        "a2a",
        "Opaque registered A2A Agent id injected post-registration; "
        "required to wire full inbound JSON-RPC in v2. v1 discovery-only card "
        "does not depend on it.",
    ),
    EnvVarSpec(
        "KSADK_A2A_ACCOUNT_ID",
        "a2a",
        "Runtime owner account id injected by the deploy layer (ar-* agent's account).",
    ),
    EnvVarSpec(
        "KSADK_A2A_RUNTIME_ID",
        "a2a",
        "Hosted Agent runtime resource id (ar-*) injected by the deploy layer; "
        "the v1 discovery-only card mounts whenever this is non-empty.",
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
        "KSADK_A2A_ENABLE_PUBLIC_EGRESS",
        "a2a",
        "Allow calling external (public-egress) agents in the A2A Space.",
        "false when the deploy layer does not inject a value",
    ),
    EnvVarSpec(
        "KSADK_A2A_EVENT_OUTBOX_PATH",
        "a2a",
        "SQLite path for durable A2A task-event delivery batches.",
        ".agentengine/a2a_event_outbox.sqlite3",
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
    EnvVarSpec(
        "KSADK_EVAL_JUDGE_API_KEY",
        "eval",
        "API key for the LLM Judge evaluation backend.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_A2UI_GENERATION_TIMEOUT_SECONDS",
        "agui",
        "A2UI structured-generation deadline in seconds; values are clamped to 1 through 120.",
        "20",
    ),
    EnvVarSpec(
        "KSADK_AICP_ENDPOINT_MODE",
        "platform",
        "AICP endpoint selection mode: auto, detect, inner, or public.",
    ),
    EnvVarSpec(
        "KSADK_ALLOWED_SUFFIXES", "builders", "Internal code package allowed suffix constant."
    ),
    EnvVarSpec(
        "KSADK_ATTACHMENT_OCR_RUNTIME_REQUIREMENTS",
        "builders",
        "Internal bundled attachment OCR requirement constant.",
    ),
    EnvVarSpec(
        "KSADK_ATTACHMENT_RUNTIME_REQUIREMENTS",
        "builders",
        "Internal bundled attachment requirement constant.",
    ),
    EnvVarSpec(
        "KSADK_BUILD_ENABLE_ATTACHMENT_OCR",
        "builders",
        "Include local attachment OCR dependencies in source builds.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_BUILD_ENABLE_MCP",
        "builders",
        "Include LangChain MCP adapter dependencies in source/container builds.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_BUILD_ENABLE_POSTGRES_SESSION",
        "builders",
        "Include asyncpg dependency for PostgreSQL session backend in source/container builds.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS",
        "builders",
        "pip install timeout seconds for source builds.",
        "2700",
    ),
    EnvVarSpec(
        "KSADK_BUILTIN_TOOLS_MODE",
        "toolsets",
        "Built-in tool injection mode: off, dispatcher, focused, or deferred.",
        "off",
    ),
    EnvVarSpec(
        "KSADK_BUILTIN_TOOLS_PROFILE",
        "toolsets",
        "Built-in tool profile selector, such as default or coding.",
        "default",
    ),
    EnvVarSpec(
        "KSADK_CHECKPOINT_BACKEND", "sessions", "LangGraph checkpoint backend selector.", "local"
    ),
    EnvVarSpec("KSADK_CHECKPOINT_PATH", "sessions", "Local SQLite checkpoint database path."),
    EnvVarSpec(
        "KSADK_CODEX_USE_PROXY",
        "codex",
        "Codex proxy override: 1 forces the local Responses-to-Chat proxy and "
        "0 forces direct mode.",
    ),
    EnvVarSpec(
        "KSADK_CODEX_SANDBOX",
        "codex",
        "Codex sandbox mode: read_only (default, no writes) / workspace_write "
        "(write inside workspace) / full_access (write anywhere).",
    ),
    EnvVarSpec(
        "KSADK_CODEX_APPROVAL",
        "codex",
        "Codex approval mode: deny_all (default for read_only) / auto_review "
        "(auto-approve with review log).",
    ),
    EnvVarSpec(
        "KSADK_CODEX_HOME",
        "codex",
        "Explicit Codex home directory override for the native runtime.",
    ),
    EnvVarSpec(
        "KSADK_CODEX_ISOLATE_HOME",
        "codex",
        "Isolate native Codex state under the project workspace; set to 0 for debugging only.",
        "1",
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
        "KSADK_COMMAND_", "sandbox", "Internal prefix for command policy environment controls."
    ),
    EnvVarSpec(
        "KSADK_COMMAND_CWD",
        "sandbox",
        "Current working directory exported to command policy checks.",
    ),
    EnvVarSpec(
        "KSADK_COMPACT_MICROCOMPACT_COLD_ROUNDS",
        "runtime",
        "Groups older than this many rounds are compacted by L3 microcompact.",
        "3",
    ),
    EnvVarSpec(
        "KSADK_COMPACT_MICROCOMPACT_ENABLED",
        "runtime",
        "Enable L3 microcompact deterministic cold-group compression in compaction pipeline.",
        "true",
    ),
    EnvVarSpec(
        "KSADK_COMPACT_SNIP_ENABLED",
        "runtime",
        "Enable L2 snip deterministic redundancy removal in compaction pipeline.",
        "true",
    ),
    *PCM_ENV_VAR_REGISTRY_ITEMS,
    EnvVarSpec(
        "KSADK_DEPLOYMENT_MODE",
        "runtime",
        "Deployment-mode ownership declaration.",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_EVAL_COMMIT",
        "evaluation",
        "Source commit recorded by evaluation runs.",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_CORE_RUNTIME_REQUIREMENTS",
        "builders",
        "Internal bundled core requirement constant.",
    ),
    EnvVarSpec("KSADK_ENABLE_MCP_TOOLS", "mcp_runtime", "Enable ADK MCP tool injection.", "1"),
    EnvVarSpec("KSADK_EVENTS_TABLE", "sessions", "Internal SQLite events table constant."),
    EnvVarSpec("KSADK_FEISHU_APP_ID", "cli", "Feishu helper app id used by OpenClaw diagnostics."),
    EnvVarSpec("KSADK_FEISHU_RESULT_PATH", "cli", "Feishu helper result file path."),
    EnvVarSpec(
        "KSADK_GLOBAL_CONFIG_ENV_KEYS",
        "cli",
        "Internal marker for env vars injected from global config.",
    ),
    EnvVarSpec(
        "KSADK_HOSTED_UI_GUIDELINES",
        "web",
        "Internal hosted-A2UI guideline constant; not a supported environment override.",
    ),
    EnvVarSpec("KSADK_KB", "knowledge_base", "AICP knowledge-base connection prefix."),
    EnvVarSpec(
        "KSADK_KB_ACCESS_KEY", "knowledge_base", "Knowledge-base API access key.", sensitive=True
    ),
    EnvVarSpec("KSADK_KB_DATASET_ID", "knowledge_base", "Knowledge-base dataset id."),
    EnvVarSpec(
        "KSADK_KB_ENDPOINT", "knowledge_base", "Knowledge-base API endpoint.", "aicp.api.ksyun.com"
    ),
    EnvVarSpec("KSADK_KB_REGION", "knowledge_base", "Knowledge-base region.", "cn-beijing-6"),
    EnvVarSpec(
        "KSADK_KB_RERANKING_ENABLE", "knowledge_base", "Enable knowledge-base reranking.", "false"
    ),
    EnvVarSpec(
        "KSADK_KB_SCORE_THRESHOLD", "knowledge_base", "Knowledge-base score threshold.", "0.0"
    ),
    EnvVarSpec(
        "KSADK_KB_SEARCH_METHOD",
        "knowledge_base",
        "Knowledge-base search method.",
        "intelligence_search",
    ),
    EnvVarSpec(
        "KSADK_KB_SECRET_KEY", "knowledge_base", "Knowledge-base API secret key.", sensitive=True
    ),
    EnvVarSpec(
        "KSADK_KB_SESSION_TOKEN",
        "knowledge_base",
        "Knowledge-base STS session token.",
        sensitive=True,
    ),
    EnvVarSpec("KSADK_KB_TOP_K", "knowledge_base", "Knowledge-base retrieval result count.", "5"),
    EnvVarSpec(
        "KSADK_LANGGRAPH_CHECKPOINT_DSN",
        "sessions",
        "LangGraph PostgreSQL checkpoint DSN.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_LANGGRAPH_AUTO_CHECKPOINT",
        "sessions",
        "Allow a hosted LangGraph runner to rebuild a factory-exported graph with the managed PostgreSQL saver.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_LOCAL_SKILLS_DIR", "skills", "Local directory containing extracted Skill packages."
    ),
    EnvVarSpec("KSADK_LTM", "memory", "AICP long-term-memory connection prefix."),
    EnvVarSpec(
        "KSADK_LTM_ACCESS_KEY", "memory", "Long-term-memory API access key.", sensitive=True
    ),
    EnvVarSpec("KSADK_LTM_AGENT_ID", "memory", "Long-term-memory agent id."),
    EnvVarSpec("KSADK_LTM_APP_NAME", "memory", "Long-term-memory application name override."),
    EnvVarSpec(
        "KSADK_LTM_AUTO_SAVE",
        "memory",
        "Auto-save completed conversation turns to SDK long-term memory.",
        "true when KSADK_LTM_BACKEND=sdk and KSADK_LTM_NAMESPACE is set",
    ),
    EnvVarSpec("KSADK_LTM_BACKEND", "memory", "Long-term-memory backend selector.", "local"),
    EnvVarSpec("KSADK_LTM_ENDPOINT", "memory", "Long-term-memory API endpoint."),
    EnvVarSpec(
        "KSADK_LTM_HTTP_TOKEN", "memory", "HTTP long-term-memory bearer token.", sensitive=True
    ),
    EnvVarSpec(
        "KSADK_LTM_HTTP_URL", "memory", "HTTP long-term-memory service URL.", sensitive=True
    ),
    EnvVarSpec("KSADK_LTM_INDEX", "memory", "Long-term-memory index name."),
    EnvVarSpec("KSADK_LTM_NAMESPACE", "memory", "Long-term-memory memory collection id."),
    EnvVarSpec("KSADK_LTM_REGION", "memory", "Long-term-memory region.", "cn-beijing-6"),
    EnvVarSpec("KSADK_LTM_SCENE_ID", "memory", "Long-term-memory scene id.", "_sys_general"),
    EnvVarSpec("KSADK_LTM_SCHEME", "memory", "Long-term-memory API scheme.", "https"),
    EnvVarSpec(
        "KSADK_LTM_SECRET_KEY", "memory", "Long-term-memory API secret key.", sensitive=True
    ),
    EnvVarSpec(
        "KSADK_LTM_SESSION_TOKEN",
        "memory",
        "Long-term-memory STS session token.",
        sensitive=True,
    ),
    EnvVarSpec("KSADK_LTM_TOP_K", "memory", "Long-term-memory retrieval result count.", "5"),
    EnvVarSpec(
        "KSADK_MCP_RUNTIME_REQUIREMENTS",
        "builders",
        "Internal bundled MCP adapter runtime requirement constant.",
    ),
    EnvVarSpec(
        "KSADK_MCP_KEY",
        "mcp_runtime",
        "MCP service API key (also reused by ksyun web search provider).",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_MCP_SERVERS", "mcp_runtime", "JSON array of MCP server configs.", sensitive=True
    ),
    EnvVarSpec("KSADK_MEMORY_BACKEND", "memory", "Generic memory backend selector.", "memory"),
    EnvVarSpec("KSADK_MEMORY_PREFIX", "memory", "Generic memory key prefix.", "ksadk:memory:"),
    EnvVarSpec("KSADK_MEMORY_TTL", "memory", "Generic memory default TTL seconds."),
    EnvVarSpec("KSADK_MEMORY_URL", "memory", "Generic memory backend URL.", sensitive=True),
    EnvVarSpec(
        "KSADK_MODEL_PROXY_AGENTS",
        "model_proxy",
        "Comma-separated agent allowlist for the experimental model proxy.",
    ),
    EnvVarSpec(
        "KSADK_MODEL_PROXY_DENY",
        "model_proxy",
        "Comma-separated agent denylist that disables the model proxy.",
    ),
    EnvVarSpec(
        "KSADK_MODEL_PROXY_ENABLED",
        "model_proxy",
        "Enable the experimental model proxy globally.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_MODEL_PROXY_MODELS",
        "model_proxy",
        "Comma-separated model allowlist for the experimental model proxy.",
    ),
    EnvVarSpec("KSADK_PG_EVENTS_TABLE", "sessions", "Internal PostgreSQL events table constant."),
    EnvVarSpec(
        "KSADK_PG_SESSIONS_TABLE", "sessions", "Internal PostgreSQL sessions table constant."
    ),
    EnvVarSpec("KSADK_PG_STATES_TABLE", "sessions", "Internal PostgreSQL states table constant."),
    EnvVarSpec(
        "KSADK_POSTGRES_SESSION_REQUIREMENTS",
        "builders",
        "Internal bundled PostgreSQL session runtime requirement constant.",
    ),
    EnvVarSpec(
        "KSADK_PROJECT_DIR", "sessions", "Project root used for local session/workspace state."
    ),
    EnvVarSpec(
        "KSADK_PROXY_TOKEN", "model_proxy", "Local Codex proxy bearer token.", sensitive=True
    ),
    EnvVarSpec(
        "KSADK_PROXY_UPSTREAM_BASE",
        "model_proxy",
        "Override URL for the Codex proxy upstream provider.",
    ),
    EnvVarSpec(
        "KSADK_PROXY_UPSTREAM_KEY",
        "model_proxy",
        "Override credential for the Codex proxy upstream provider.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_PUBLIC_SKILL_ALLOWLIST",
        "skills",
        "Comma-separated public Skill names to load; empty loads all public skills.",
    ),
    EnvVarSpec(
        "KSADK_PUBLIC_SKILL_SPACE_IDS",
        "skills",
        "Comma-separated public Skill Space ids appended after user spaces.",
    ),
    EnvVarSpec(
        "KSADK_RESPONSES_SESSION_HEADER",
        "runners",
        "Header name for remote Responses session propagation.",
    ),
    EnvVarSpec(
        "KSADK_RUNTIME_IMAGE_SOURCE_COMMIT",
        "runtime",
        "Build-injected source commit for Runtime image provenance.",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_RUNTIME_IMAGE_WHEEL_SHA256",
        "runtime",
        "Build-injected wheel digest for Runtime image provenance.",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_RUNTIME_PORT", "cli", "Runtime HTTP port exported to template runtimes.", "8080"
    ),
    EnvVarSpec(
        "KSADK_RUNTIME_REQUIREMENTS", "builders", "Internal bundled runtime requirements constant."
    ),
    EnvVarSpec(
        "KSADK_RUNTIME_STATE_DIR",
        "runtime",
        "Internal Runtime state directory override.",
        documented=False,
    ),
    EnvVarSpec(
        "KSADK_ALLOW_POD_PROCESS_TOOLS",
        "sandbox",
        "Explicit opt-in required before pod_process sandbox tools are enabled.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_MAX_TURNS",
        "runtime",
        "Maximum conversation turns before runtime circuit breaker opens.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_MAX_TOOL_CALLS",
        "runtime",
        "Maximum tool calls before runtime circuit breaker opens.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_MAX_CONSECUTIVE_TOOL_FAILURES",
        "runtime",
        "Maximum consecutive failed tool results before runtime circuit breaker opens.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_MAX_CONSECUTIVE_APPROVAL_DENIALS",
        "runtime",
        "Maximum consecutive approval denials before runtime circuit breaker opens.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_MAX_CONSECUTIVE_COMPACT_FAILURES",
        "runtime",
        "Maximum consecutive compaction failures before runtime circuit breaker opens.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_MAX_CONSECUTIVE_SEMANTIC_FAILURES",
        "runtime",
        "Consecutive semantic LLM compaction failures before using extractive; "
        "0 disables the breaker to avoid permanent disable from transient failures.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_SAFE_", "tools", "Internal prefix for tool safety policy environment controls."
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_ALLOW_INTERNET_ACCESS",
        "sandbox",
        "Allow remote sandbox internet access.",
        "true",
    ),
    EnvVarSpec("KSADK_SANDBOX_BACKEND", "sandbox", "Generic sandbox backend selector.", "e2b"),
    EnvVarSpec(
        "KSADK_SANDBOX_IDLE_TTL_SECONDS",
        "sandbox",
        "Idle TTL seconds before sandbox registry reclaims inactive sessions; "
        "0 disables idle reclamation.",
        "300",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_MAX_SESSIONS", "sandbox", "Maximum active sandbox registry sessions.", "0"
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_SESSION_ID", "sandbox", "Explicit sandbox registry session id override."
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_STARTUP_RETRY_ATTEMPTS",
        "sandbox",
        "Sandbox startup readiness retry attempts.",
        "6",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_STARTUP_RETRY_DELAY",
        "sandbox",
        "Sandbox startup readiness initial retry delay seconds.",
        "0.2",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS",
        "sandbox",
        "Background sweep interval seconds for sandbox registry; "
        "0 disables the background sweeper.",
        "60",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_SYNC_MAX_FILE_BYTES",
        "sandbox",
        "Maximum single file size for workspace sync into sandbox.",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_SYNC_MAX_FILES",
        "sandbox",
        "Maximum file count for workspace sync into sandbox.",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_SYNC_MAX_TOTAL_BYTES",
        "sandbox",
        "Maximum total bytes for workspace sync into sandbox.",
    ),
    EnvVarSpec("KSADK_SANDBOX_TEMPLATE_ID", "sandbox", "Sandbox console template id."),
    EnvVarSpec("KSADK_SANDBOX_TIMEOUT", "sandbox", "Sandbox session timeout seconds.", "600"),
    EnvVarSpec(
        "KSADK_SANDBOX_TTL_SECONDS",
        "sandbox",
        "Hard TTL seconds for sandbox registry sessions.",
        "900",
    ),
    EnvVarSpec(
        "KSADK_SANDBOX_TYPE", "sandbox", "Sandbox type: aio, code, browser, or private.", "aio"
    ),
    EnvVarSpec(
        "KSADK_SELECTED_SKILL_NAMES",
        "skills",
        "Comma-separated Skill names selected by the outer agent.",
    ),
    EnvVarSpec("KSADK_SESSIONS_TABLE", "sessions", "Internal SQLite sessions table constant."),
    EnvVarSpec(
        "KSADK_SESSION_BACKEND", "sessions", "Conversation session backend selector.", "local"
    ),
    EnvVarSpec(
        "KSADK_SESSION_CONNECT_TIMEOUT",
        "sessions",
        "Conversation PostgreSQL connection timeout seconds.",
        "5",
    ),
    EnvVarSpec(
        "KSADK_SESSION_DSN", "sessions", "Conversation session database DSN.", sensitive=True
    ),
    EnvVarSpec("KSADK_SESSION_NAMESPACE", "sessions", "Conversation session namespace."),
    EnvVarSpec(
        "KSADK_AGENT_ID",
        "platform",
        "Stable AgentEngine agent identity used only as a fallback checkpoint namespace.",
    ),
    EnvVarSpec(
        "KSADK_AGENT_KERNEL",
        "kernel",
        "Opt in to Agent Kernel ingress locally; managed deployment may use AGENT_KERNEL_ENABLED instead.",
        "false",
    ),
    EnvVarSpec("KSADK_SESSION_PATH", "sessions", "Conversation local SQLite database path."),
    EnvVarSpec(
        "KSADK_SESSION_PG_CONNECT_TIMEOUT",
        "sessions",
        "Legacy PostgreSQL session connection timeout seconds.",
        "5",
    ),
    EnvVarSpec(
        "KSADK_SKILLS_MODE", "skills", "Skill loading mode: auto, local, or sandbox.", "auto"
    ),
    EnvVarSpec(
        "KSADK_SKILL_ALLOW_HASH_MISMATCH",
        "skills",
        "Allow loading legacy Skill archives when ContentHash verification fails.",
        "false",
    ),
    EnvVarSpec(
        "KSADK_SKILL_ARTIFACT_PROJECT",
        "skills",
        "Default artifact project name for the minimal Skill Runtime agent.",
        "ksadk-artifact",
    ),
    EnvVarSpec(
        "KSADK_SKILL_CACHE_DIR", "skills", "Skill package download and extraction cache directory."
    ),
    EnvVarSpec(
        "KSADK_SKILL_MANIFEST_LIMIT",
        "skills",
        "Maximum remote Skill manifests injected into agent instructions.",
        "30",
    ),
    EnvVarSpec(
        "KSADK_SKILL_MANIFEST_TIMEOUT",
        "skills",
        "Remote Skill manifest listing timeout seconds.",
        "5",
    ),
    EnvVarSpec(
        "KSADK_SKILL_OUTPUT_DIR",
        "skills",
        "Output directory exposed to local Skill workflow scripts.",
    ),
    EnvVarSpec(
        "KSADK_SKILL_ROOT_DIR",
        "skills",
        "Root directory of the Skill currently executed by the local workflow runner.",
    ),
    EnvVarSpec(
        "KSADK_SKILL_RUNTIME_AGENT_PATH", "skills", "Local process Skill Runtime agent path."
    ),
    EnvVarSpec(
        "KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS",
        "skills",
        "Allow remote Skill Runtime internet access.",
        "true",
    ),
    EnvVarSpec(
        "KSADK_SKILL_RUNTIME_BACKEND", "skills", "Skill Runtime backend selector.", "disabled"
    ),
    EnvVarSpec("KSADK_SKILL_RUNTIME_TEMPLATE_ID", "skills", "Skill Runtime backend template id."),
    EnvVarSpec(
        "KSADK_SKILL_RUNTIME_TIMEOUT", "skills", "Skill Runtime workflow timeout seconds.", "900"
    ),
    EnvVarSpec(
        "KSADK_SKILL_SERVICE", "skills", "Skill Service AICP connection environment prefix."
    ),
    EnvVarSpec(
        "KSADK_SKILL_SERVICE_ACCESS_KEY", "skills", "Skill Service KOP access key.", sensitive=True
    ),
    EnvVarSpec("KSADK_SKILL_SERVICE_ACCOUNT_ID", "skills", "Skill Service tenant account id."),
    EnvVarSpec(
        "KSADK_SKILL_SERVICE_API_VERSION", "skills", "Skill Service KOP API version.", "2024-06-12"
    ),
    EnvVarSpec("KSADK_SKILL_SERVICE_ENDPOINT", "skills", "Skill Service AICP endpoint override."),
    EnvVarSpec("KSADK_SKILL_SERVICE_REGION", "skills", "Skill Service KOP region.", "cn-beijing-6"),
    EnvVarSpec("KSADK_SKILL_SERVICE_SCHEME", "skills", "Skill Service AICP URL scheme override."),
    EnvVarSpec(
        "KSADK_SKILL_SERVICE_SECRET_KEY", "skills", "Skill Service KOP secret key.", sensitive=True
    ),
    EnvVarSpec(
        "KSADK_SKILL_SERVICE_SIGN_SERVICE", "skills", "Skill Service KOP signing service.", "aicp"
    ),
    EnvVarSpec(
        "KSADK_SKILL_SERVICE_TOKEN", "skills", "Skill Service bearer token.", sensitive=True
    ),
    EnvVarSpec("KSADK_SKILL_SERVICE_URL", "skills", "Skill Service API base URL."),
    EnvVarSpec("KSADK_SKILL_SPACE_IDS", "skills", "Comma-separated Skill Space ids."),
    EnvVarSpec(
        "KSADK_SKILL_WORKDIR", "skills", "Working directory for the minimal Skill Runtime agent."
    ),
    EnvVarSpec("KSADK_STATES_TABLE", "sessions", "Internal SQLite states table constant."),
    EnvVarSpec("KSADK_STM_BACKEND", "sessions", "Short-term-memory session backend selector."),
    EnvVarSpec("KSADK_STM_DB_PATH", "sessions", "Legacy short-term-memory SQLite path."),
    EnvVarSpec(
        "KSADK_STM_DB_URL", "sessions", "Legacy short-term-memory database URL.", sensitive=True
    ),
    EnvVarSpec("KSADK_STM_PATH", "sessions", "Short-term-memory SQLite path."),
    EnvVarSpec("KSADK_STM_URL", "sessions", "Short-term-memory database URL.", sensitive=True),
    EnvVarSpec("KSADK_TENANT_ID", "sessions", "Tenant id used for session namespace scoping."),
    EnvVarSpec(
        "KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST",
        "terminal",
        "Comma-separated remote terminal exec prefixes appended to the default allowlist; "
        "use * to allow all.",
    ),
    EnvVarSpec(
        "KSADK_TOOL_APPROVAL_MODE",
        "tools",
        "Built-in tool approval mode: ask, risk, or full.",
        "risk",
    ),
    EnvVarSpec(
        "KSADK_TOOL_RESULT_DIR", "tools", "Directory used to persist oversized tool results."
    ),
    EnvVarSpec(
        "KSADK_TOOL_RESULT_MAX_CHARS",
        "tools",
        "Maximum inline characters for budgeted tool outputs.",
    ),
    EnvVarSpec(
        "KSADK_TOOL_RESULT_PERSIST_THRESHOLD_CHARS",
        "tools",
        "Character threshold for persisting tool outputs.",
    ),
    EnvVarSpec(
        "KSADK_TOOL_RESULT_PREVIEW_CHARS",
        "tools",
        "Preview character count for persisted tool outputs.",
    ),
    EnvVarSpec("KSADK_UPDATED_AT", "configs", "Internal config update timestamp field."),
    EnvVarSpec("KSADK_VERSION", "configs", "Internal config version field."),
    EnvVarSpec(
        "KSADK_UI_BUNDLE_PATH", "web", "Custom agent UI bundle path relative to project root."
    ),
    EnvVarSpec("KSADK_UI_PATH", "web", "Custom agent UI mount path."),
    EnvVarSpec("KSADK_UI_PROFILE", "web", "Agent UI profile selector, such as builtin or custom."),
    EnvVarSpec("KSADK_UI_URL", "web", "External custom agent UI URL."),
    EnvVarSpec(
        "KSADK_WEB_CACHE_DIR", "web", "Directory used by hosted Web UI static asset sync cache."
    ),
    EnvVarSpec(
        "KSADK_WEB_PACKAGE", "web", "KsADK Web npm package name.", "@kingsoftcloud/ksadk-web"
    ),
    EnvVarSpec("KSADK_WEB_RELEASE_URL", "web", "Optional KsADK Web tarball URL fallback."),
    EnvVarSpec(
        "KSADK_WEB_SEARCH_API_KEY", "web", "HTTP web search provider API key.", sensitive=True
    ),
    EnvVarSpec("KSADK_WEB_SEARCH_BASE_URL", "web", "HTTP web search provider base URL."),
    EnvVarSpec(
        "KSADK_WEB_SEARCH_PROVIDER",
        "web",
        "Web search provider selector, such as fake, http, or ksyun.",
    ),
    EnvVarSpec(
        "KSADK_WEB_SEARCH_SCOPE",
        "web",
        "ksyun provider search scope: webpage/document/scholar/podcast/video.",
        "webpage",
    ),
    EnvVarSpec(
        "KSADK_WEB_SSRF_POLICY_JSON", "web", "JSON policy overrides for web_fetch SSRF checks."
    ),
    EnvVarSpec("KSADK_WEB_TARBALL_NAME", "web", "KsADK Web fallback tarball file name."),
    EnvVarSpec(
        "KSADK_WEB_VERSION",
        "web",
        "Published KsADK Web npm version used for a reproducible wheel build.",
        "0.3.2",
    ),
    EnvVarSpec(
        "KSADK_WORKING_SET_MAX_FILES",
        "runtime",
        "Maximum recent files recorded in compaction working set metadata.",
        "5",
    ),
    EnvVarSpec(
        "KSADK_USER_BACKEND_URL", "web", "User-facing backend URL used by hosted UI integrations."
    ),
    EnvVarSpec(
        "KSADK_WORKFLOW_PROMPT", "skills", "Prompt text exposed to local Skill workflow scripts."
    ),
    EnvVarSpec(
        "KSADK_WORKSPACE_ID", "sessions", "Workspace id used for session namespace scoping."
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_APP_KEY",
        "tracing",
        "Deprecated: CloudMonitor AppKey translated to Ksc-Appkey OTLP header only when "
        "both CLOUD_MONITOR_OTLP_TRACES_HEADERS and CLOUD_MONITOR_OTLP_HEADERS are absent. "
        "Server should inject OTLP headers directly.",
        sensitive=True,
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_OTLP_ENDPOINT",
        "tracing",
        "CloudMonitor generic OTLP HTTP endpoint; KsADK derives /v1/traces when no "
        "traces endpoint is set.",
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_OTLP_HEADERS",
        "tracing",
        "CloudMonitor OTLP HTTP headers including Ksc-Appkey, comma-separated and URL-encoded.",
        sensitive=True,
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_OTLP_PROTOCOL",
        "tracing",
        "CloudMonitor generic OTLP protocol; KsADK supports http/protobuf.",
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT",
        "tracing",
        "CloudMonitor OTLP HTTP traces endpoint; takes precedence over the generic endpoint.",
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_OTLP_TRACES_HEADERS",
        "tracing",
        "CloudMonitor OTLP HTTP traces headers; takes precedence over generic headers.",
        sensitive=True,
    ),
    EnvVarSpec(
        "CLOUD_MONITOR_OTLP_TRACES_PROTOCOL",
        "tracing",
        "CloudMonitor traces protocol; takes precedence over the generic protocol.",
    ),
    EnvVarSpec(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "tracing",
        "Generic OTLP HTTP endpoint used to derive the traces endpoint.",
    ),
    EnvVarSpec(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "tracing",
        "Generic OTLP HTTP headers, comma-separated and URL-encoded.",
        sensitive=True,
    ),
    EnvVarSpec(
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "tracing",
        "Generic OTLP protocol; KsADK auto HTTP exporter supports http/protobuf.",
    ),
    EnvVarSpec(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "tracing",
        "OTLP HTTP traces endpoint; takes precedence over the generic endpoint.",
    ),
    EnvVarSpec(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "tracing",
        "OTLP HTTP traces headers; takes precedence over generic headers.",
        sensitive=True,
    ),
    EnvVarSpec(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "tracing",
        "OTLP traces protocol; takes precedence over the generic protocol.",
    ),
    EnvVarSpec(
        "OTEL_RESOURCE_ATTRIBUTES",
        "tracing",
        "OpenTelemetry resource attributes in key=value comma-separated form.",
    ),
    EnvVarSpec("OTEL_SERVICE_NAME", "tracing", "OpenTelemetry service name."),
    EnvVarSpec(
        "OPENCLAW_CONFIG_PATCH_JSON",
        "openclaw",
        "OpenClaw configuration patch JSON supplied at deployment time.",
        sensitive=True,
    ),
    EnvVarSpec(
        "KSADK_OTLP_MAX_EXPORT_BATCH_SIZE",
        "tracing",
        "Maximum spans exported per OTLP batch to avoid oversized collector requests.",
        "64",
    ),
)

ENV_VAR_REGISTRY: tuple[EnvVarSpec, ...] = tuple(
    sorted(_ENV_VAR_REGISTRY_ITEMS, key=lambda spec: spec.name)
)
_ENV_SENSITIVITY_BY_NAME = {spec.name: spec.sensitive for spec in ENV_VAR_REGISTRY}


def is_sensitive_env_var(name: str) -> bool:
    normalized = str(name or "").strip().upper()
    if _ENV_SENSITIVITY_BY_NAME.get(normalized, False):
        return True
    if normalized.startswith("OTEL_EXPORTER_OTLP_") and normalized.endswith("_HEADERS"):
        return True
    return any(
        token in normalized
        for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "SIGNATURE")
    )


def iter_env_vars() -> tuple[EnvVarSpec, ...]:
    return ENV_VAR_REGISTRY
