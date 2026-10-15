"""Host-pinned task policies layered above generic tool and budget execution.

Web observation rules are activated by an explicit configuration. The Kernel
contains no implicit extension table or model-authored policy authority.
"""

from .ports import RunConfiguration, ToolResult, WorkspaceObservationPolicy
from .state import RunSnapshot


def invalidates_browser_observation(
    result: ToolResult, policy: WorkspaceObservationPolicy | None
) -> bool:
    return bool(
        policy
        and any(
            path.lower().endswith(policy.path_suffixes) for path in result.workspace_changed_paths
        )
    )


def browser_observation_available(config: RunConfiguration) -> bool:
    """Registered capabilities do not override the active goal's tool scope."""
    allowed = config.contract.allowed_tool_names
    return any(
        tool.observation_role == "browser" and (allowed is None or tool.name in allowed)
        for tool in config.tools
    )


def web_verification_only(snapshot: RunSnapshot, config: RunConfiguration) -> bool:
    """Keep observation available after the last allowed late web mutation.

    This gate is activated only for a Run that actually changed web files and
    has a browser observation port. The preceding call can still repair; the
    final tool-capable call cannot make its own browser evidence stale.
    """
    if config.workspace_observation_policy is None:
        return False
    reserve = config.limits.verification_reserve_model_calls
    remaining = (
        config.limits.max_model_calls - snapshot.model_budget_claimed
    )
    return bool(
        reserve > 1
        and config.limits.finalization_reserve_model_calls > 0
        and snapshot.last_web_mutation_tool_call is not None
        and browser_observation_available(config)
        and 0 < remaining < reserve
        and (
            reserve < config.limits.finalization_reserve_model_calls + 4
            or remaining <= config.limits.finalization_reserve_model_calls + 1
        )
        and remaining > config.limits.finalization_reserve_model_calls
    )


def web_observation_due(snapshot: RunSnapshot, config: RunConfiguration) -> bool:
    """Require browser attempts before prolonged blind edits and near the limit.

    A failed browser attempt counts as an attempt, not as successful evidence.
    It unlocks the remaining repair window; later web edits can require a new
    attempt. The final tool-capable turn still requires a fresh observation.
    """
    if config.workspace_observation_policy is None:
        return False
    limits = config.limits
    reserve = limits.verification_reserve_model_calls
    interval = limits.browser_observation_interval_tool_calls
    remaining = limits.max_model_calls - snapshot.model_budget_claimed
    last_mutation = snapshot.last_web_mutation_tool_call
    if (
        last_mutation is None
        or remaining <= limits.finalization_reserve_model_calls
        or not browser_observation_available(config)
    ):
        return False
    observed = snapshot.last_browser_observation
    unresolved = snapshot.unresolved_interaction_observation
    last_attempt = max(
        (
            item
            for item in (
                snapshot.last_browser_attempt_tool_call,
                *(
                    entry["tool_call"]
                    for entry in (observed, unresolved)
                    if isinstance(entry, dict) and type(entry.get("tool_call")) is int
                ),
            )
            if type(item) is int
        ),
        default=-1,
    )
    if last_attempt >= last_mutation:
        return False
    early_due = interval > 0 and snapshot.tool_calls - max(last_attempt, 0) >= interval
    reserved_due = reserve >= limits.finalization_reserve_model_calls + 4 and remaining <= reserve
    return early_due or reserved_due
