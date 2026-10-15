"""Validate Native Studio opt-in combinations before workspace side effects."""

from ksadk.harness.native.ports import CHILD_RECOVERY_POLICIES
from ksadk.studio.native_model_recovery import StudioModelTakeoverDeclaration


def validate_native_options(
    *,
    selected_native_agents,
    native_harness_default=False,
    native_harness_durable,
    native_harness_session_context_read,
    native_harness_browser_reobserve,
    native_harness_terminal_child_reconcile,
    native_harness_prepared_child_recovery,
    native_harness_child_recovery_policy,
    native_harness_model_recovery_policy,
    native_harness_model_recovery_host,
    harness_engine_selector,
    native_acceptance_extension,
    native_delivery_review_policy,
) -> None:
    native_enabled = native_harness_default or bool(selected_native_agents)
    if native_harness_durable and not native_enabled:
        raise ValueError("Native durable storage requires selected Studio agents")
    if type(native_harness_session_context_read) is not bool:
        raise ValueError("Native session context read must be a boolean")
    if native_harness_session_context_read and not native_enabled:
        raise ValueError("Native session context read requires selected Studio agents")
    if type(native_harness_browser_reobserve) is not bool:
        raise ValueError("Native browser reobservation must be a boolean")
    if native_harness_browser_reobserve and not native_enabled:
        raise ValueError("Native browser reobservation requires selected Studio agents")
    if type(native_harness_terminal_child_reconcile) is not bool:
        raise ValueError("Native child reconciliation must be a boolean")
    if native_harness_terminal_child_reconcile and (
        not native_enabled or not native_harness_durable
    ):
        raise ValueError("Native child reconciliation requires selected durable Studio agents")
    if type(native_harness_prepared_child_recovery) is not bool:
        raise ValueError("Native prepared child recovery must be a boolean")
    if native_harness_prepared_child_recovery and (
        not native_enabled or not native_harness_durable
    ):
        raise ValueError("Native prepared child recovery requires selected durable Studio agents")
    if native_harness_prepared_child_recovery and native_harness_terminal_child_reconcile:
        raise ValueError("select one Native child recovery policy")
    if native_harness_child_recovery_policy is not None:
        if (
            type(native_harness_child_recovery_policy) is not str
            or native_harness_child_recovery_policy not in CHILD_RECOVERY_POLICIES
        ):
            raise ValueError("Native child recovery policy is invalid")
        if not native_enabled or not native_harness_durable:
            raise ValueError("Native child recovery requires selected durable Studio agents")
        if native_harness_prepared_child_recovery or native_harness_terminal_child_reconcile:
            raise ValueError("select one Native child recovery policy")
    if native_harness_model_recovery_policy is not None:
        if (
            native_harness_model_recovery_policy != "pure-inference-takeover/v1"
            or not native_enabled
            or not native_harness_durable
            or not isinstance(native_harness_model_recovery_host, StudioModelTakeoverDeclaration)
        ):
            raise ValueError("Native model takeover requires declared selected durable agents")
    elif native_harness_model_recovery_host is not None:
        raise ValueError("model takeover declaration requires explicit policy")
    if native_enabled and harness_engine_selector is not None:
        raise ValueError("Native Studio agent selection conflicts with custom builder")
    if native_acceptance_extension is not None and not native_enabled:
        raise ValueError("Native acceptance extension requires selected Studio agents")
    if native_delivery_review_policy is not None and not native_enabled:
        raise ValueError("Native delivery review policy requires selected Studio agents")
    if any(item == "*" for item in selected_native_agents):
        raise ValueError("Native Studio rollout requires exact Agent IDs")
