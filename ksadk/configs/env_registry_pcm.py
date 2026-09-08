"""Prompt, Context and Memory environment-variable registry entries."""

from __future__ import annotations

from dataclasses import replace

from ksadk.configs.env_var_spec import EnvVarSpec

_PCM_ENV_VAR_REGISTRY_ITEMS: tuple[EnvVarSpec, ...] = (
    EnvVarSpec("KSADK_BASELINE_COLLECT", "context", "Enable PCM baseline collection.", "0"),
    EnvVarSpec(
        "KSADK_BASELINE_EXECUTION_TARGET", "context", "PCM baseline execution target label."
    ),
    EnvVarSpec(
        "KSADK_BASELINE_FLUSH_EACH_TURN",
        "context",
        "Flush PCM baseline output after every turn.",
        "0",
    ),
    EnvVarSpec("KSADK_BASELINE_PATH", "context", "PCM baseline JSONL output path."),
    EnvVarSpec("KSADK_COMPACT_HARD_LIMIT_PCT", "context", "Hard compaction threshold percentage."),
    EnvVarSpec(
        "KSADK_COMPACT_HARD_LIMIT_PCT_DEFAULT",
        "context",
        "Default hard compaction threshold percentage.",
    ),
    EnvVarSpec("KSADK_COMPACT_SOFT_LIMIT_PCT", "context", "Soft compaction threshold percentage."),
    EnvVarSpec(
        "KSADK_COMPACT_SOFT_LIMIT_PCT_DEFAULT",
        "context",
        "Default soft compaction threshold percentage.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_CACHE_BREAK_OBSERVABILITY",
        "context",
        "Enable prompt cache-break diagnostics.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_CONTRIBUTOR_ALLOW_PLATFORM_TRUST",
        "context",
        "Allow trusted platform context contributors.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_CONTRIBUTOR_FAILURE_MODE",
        "context",
        "Context contributor failure policy.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_CONTRIBUTOR_TIMEOUT_MS",
        "context",
        "Context contributor timeout in milliseconds.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_EMERGENCY_KEEP_TAIL_GROUPS",
        "context",
        "Recent event groups retained during emergency compaction.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_ENGINE_V2_ENABLED", "context", "Enable the PCM context planner.", "0"
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_HARD_LIMIT_PERCENT",
        "context",
        "Hard request-context budget threshold percentage.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_KEEP_TAIL_GROUPS",
        "context",
        "Recent event groups retained during normal compaction.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_MAX_RETRY_AFTER_PTL",
        "context",
        "Maximum controlled retries after prompt-too-long.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_RULE_FILES_MAX_TOKENS", "context", "Combined rule-file token budget."
    ),
    EnvVarSpec("KSADK_CONTEXT_RULE_FILE_MAX_TOKENS", "context", "Per rule-file token budget."),
    EnvVarSpec(
        "KSADK_CONTEXT_SAFETY_BUFFER_TOKENS", "context", "Reserved context-window safety buffer."
    ),
    EnvVarSpec("KSADK_CONTEXT_SEMANTIC_ENABLED", "context", "Enable semantic compaction.", "0"),
    EnvVarSpec(
        "KSADK_CONTEXT_SEMANTIC_TIMEOUT_MS",
        "context",
        "Semantic compaction timeout in milliseconds.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_SOFT_LIMIT_PERCENT",
        "context",
        "Soft request-context budget threshold percentage.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_TOOL_RESULT_MAX_TOKENS",
        "context",
        "Maximum token budget for one tool result.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_WORKING_STATE_ENABLED",
        "context",
        "Enable structured working-state extraction.",
        "0",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_WORKING_STATE_EXTRACTION_TIMEOUT_MS",
        "context",
        "Working-state extraction timeout in milliseconds.",
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_WORKING_STATE_MAX_TOKENS", "context", "Working-state token budget."
    ),
    EnvVarSpec(
        "KSADK_CONTEXT_WORKING_STATE_MIN_TOKEN_GROWTH",
        "context",
        "Minimum growth before refreshing working state.",
    ),
    EnvVarSpec(
        "KSADK_LTM_FORCE_INMEMORY",
        "memory",
        "Force in-memory long-term-memory backend for tests.",
        "0",
    ),
    EnvVarSpec("KSADK_MEMORY_CORE_MAX_TOKENS", "memory", "Core-memory token budget."),
    EnvVarSpec("KSADK_MEMORY_DB_PATH", "memory", "Local PCM memory database path."),
    EnvVarSpec("KSADK_MEMORY_ENABLED", "memory", "Enable platform memory projection.", "0"),
    EnvVarSpec(
        "KSADK_MEMORY_FLUSH_BEFORE_COMPACTION",
        "memory",
        "Flush memory candidates before compaction.",
        "0",
    ),
    EnvVarSpec("KSADK_MEMORY_FLUSH_ENABLED", "memory", "Enable memory candidate commit.", "0"),
    EnvVarSpec("KSADK_MEMORY_MIN_SCORE", "memory", "Minimum memory recall relevance score."),
    EnvVarSpec(
        "KSADK_MEMORY_MAX_RECORDS",
        "memory",
        "Maximum retained records for the local PCM memory provider.",
        "10000",
    ),
    EnvVarSpec("KSADK_MEMORY_PROVIDER", "memory", "Platform memory provider selector."),
    EnvVarSpec("KSADK_MEMORY_RECALL_MAX_TOKENS", "memory", "Memory recall token budget."),
    EnvVarSpec("KSADK_MEMORY_RECALL_TOP_K", "memory", "Maximum recalled memory items."),
    EnvVarSpec(
        "KSADK_MEMORY_RETENTION_DAYS",
        "memory",
        "Retention period in days for the local PCM memory provider.",
        "90",
    ),
    EnvVarSpec(
        "KSADK_MEMORY_WRITE_MODE",
        "memory",
        "Memory write mode: off, explicit-only, or candidate.",
    ),
    EnvVarSpec(
        "KSADK_PLATFORM_SAFETY_TEXT",
        "prompt",
        "Platform safety rules injected by the prompt compiler.",
    ),
    EnvVarSpec(
        "KSADK_PROMPT_AUTO_DISCOVERY", "prompt", "Enable project prompt-source discovery.", "0"
    ),
    EnvVarSpec(
        "KSADK_PROMPT_COMPILER_ENABLED", "prompt", "Enable structured prompt compilation.", "0"
    ),
    EnvVarSpec(
        "KSADK_TOKENIZER_PROVIDER", "context", "Tokenizer provider used for context accounting."
    ),
)

# PCM rollout, budget and diagnostic environment variables are internal runtime
# controls. Public users configure the same behavior through AgentSpec policies,
# so these names intentionally do not expand the public environment reference.
PCM_ENV_VAR_REGISTRY_ITEMS: tuple[EnvVarSpec, ...] = tuple(
    replace(item, documented=False) for item in _PCM_ENV_VAR_REGISTRY_ITEMS
)


__all__ = ["PCM_ENV_VAR_REGISTRY_ITEMS"]
