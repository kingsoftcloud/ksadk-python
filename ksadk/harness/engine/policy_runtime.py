"""Run-local host authority: isolate grants and revalidate before side effects."""

from __future__ import annotations

from dataclasses import replace

from ksadk.harness.execution_policy import ExecutionPolicy, apply_execution_policy
from ksadk.harness.subagent import SubAgentSpec


def configure_run(engine, run, *, policy=None, resolver=None, request=None):
    inherited = getattr(engine, "_inherited_policy_tool_names", None)
    if policy is not None and inherited is None:
        run.compiled = replace(run.compiled, spec=apply_execution_policy(run.compiled.spec, policy))
    run.execution_policy = policy
    run.execution_policy_resolver = resolver
    run.execution_policy_request = request
    run.tools = dict(engine._tools)
    run.policy_tool_names = set(inherited or ())
    if policy is not None and inherited is None:
        run.tools.update(policy.tools)
        run.policy_tool_names.update(policy.tools)
    run.approval_required = set(engine._approval_required)
    if policy is not None:
        run.approval_required.update(policy.approval_required)
    run.sub_agents = {
        **engine._sub_agents,
        **{b.name: SubAgentSpec.from_binding(b) for b in run.compiled.spec.sub_agents},
    }
    run.controller = engine._new_run_controller(run.compiled)


async def revalidate_policy(engine, run):
    resolver, request = run.execution_policy_resolver, run.execution_policy_request
    if resolver is None:
        return
    policy = await resolver.resolve(request.metadata["execution_policy_ref"], request=request)
    if not isinstance(policy, ExecutionPolicy):
        raise TypeError("host resolver must return ExecutionPolicy")
    # A refreshed grant can narrow an admitted run, never grow its tool surface.
    allowed = {name: tool for name, tool in engine._tools.items()
               if name not in run.policy_tool_names}
    allowed.update({name: tool for name, tool in policy.tools.items()
                    if name in run.policy_tool_names})
    run.tools = {name: allowed[name] for name in run.tools if name in allowed}
    run.approval_required.update(policy.approval_required)
    limits_only = ExecutionPolicy(limits=policy.limits)
    run.compiled = replace(
        run.compiled, spec=apply_execution_policy(run.compiled.spec, limits_only)
    )
    run.execution_policy = policy
