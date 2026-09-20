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
    run.exclusive_tools = bool(policy and policy.exclusive_tools)
    run.tools = {} if run.exclusive_tools and inherited is None else dict(engine._tools)
    run.policy_tool_names = set(inherited or ())
    if policy is not None and inherited is None:
        run.tools.update(policy.tools)
        run.policy_tool_names.update(policy.tools)
    run.approval_required = set(engine._approval_required)
    if policy is not None:
        run.approval_required.update(policy.approval_required)
    _apply_request_approval_mode(run, request)
    _apply_request_approval_mode(run, request)
    run.sub_agents = {
        **engine._sub_agents,
        **{b.name: SubAgentSpec.from_binding(b) for b in run.compiled.spec.sub_agents},
    }
    if run.exclusive_tools:
        run.sub_agents = {}
    run.controller = engine._new_run_controller(run.compiled)


async def revalidate_policy(engine, run):
    resolver, request = run.execution_policy_resolver, run.execution_policy_request
    if resolver is None:
        return
    policy = await resolver.resolve(request.metadata["execution_policy_ref"], request=request)
    if not isinstance(policy, ExecutionPolicy):
        raise TypeError("host resolver must return ExecutionPolicy")
    # A refreshed grant can narrow an admitted run, never grow its tool surface.
    run.exclusive_tools = run.exclusive_tools or policy.exclusive_tools
    if run.exclusive_tools:
        run.sub_agents = {}
    allowed = {
        name: tool
        for name, tool in engine._tools.items()
        if name not in run.policy_tool_names and not run.exclusive_tools
    }
    allowed.update(
        {name: tool for name, tool in policy.tools.items() if name in run.policy_tool_names}
    )
    run.tools = {name: allowed[name] for name in run.tools if name in allowed}
    run.approval_required.update(policy.approval_required)
    limits_only = ExecutionPolicy(limits=policy.limits)
    run.compiled = replace(
        run.compiled, spec=apply_execution_policy(run.compiled.spec, limits_only)
    )
    run.execution_policy = policy


def _apply_request_approval_mode(run, request) -> None:
    """回合级审批档位（composer 的 完全访问/严格/询问）覆盖静态合同。

    ``full`` = 免确认（用户已明确授权本次运行的工具副作用）；
    ``ask`` = 全部需要确认；缺省保持合同与执行策略的静态判定。
    """
    request = request or getattr(run, "execution_policy_request", None)
    mode = ""
    if request is not None:
        mode = str((getattr(request, "metadata", None) or {}).get("tool_approval_mode") or "")
    if mode == "full":
        run.approval_required = set()
    elif mode == "ask":
        run.approval_required.update(name for name in getattr(run, "tools", {}) or {})
