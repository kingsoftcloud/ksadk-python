"""Optional SubagentProvider implementations.

Subagents are child executions selected by a parent agent.  They are not
top-level AgentProviders and therefore live outside ``plugins.providers``.
"""

from ksadk.plugins.subagent_providers.codex import (
    DEFAULT_CODEX_CHILD_PROVIDER_REF,
    CodexOneShotSubagentProvider,
)

__all__ = ["CodexOneShotSubagentProvider", "DEFAULT_CODEX_CHILD_PROVIDER_REF"]
