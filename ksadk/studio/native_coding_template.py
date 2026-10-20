"""Reviewable Native coding assembly with explicit model and browser feedback.

ZCode's control-browser/web-gui-tester methodology is adapted to Native's
bound browser schema and progressive Skill reads. It grants no host permission.
"""

import yaml

from ksadk.conversations.model_context import get_context_window_tokens, supports_native_image_input
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    AgentTemplateComposeRequest,
    AgentTemplateComposition,
    AgentTemplateRecommendation,
    CapabilityBinding,
    ContextContributorsSpec,
    ContextSpec,
    ExecutionSpec,
    Instructions,
    ModelSpec,
    RuntimeRef,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.templates import _select_model, _select_model_ids, _select_resources
from ksadk.studio.workspace import Workspace

SKILL_NAME = "native-browser-verification"
_TOOLS = (
    "list_workspace_files",
    "read_workspace_file",
    "search_workspace_files",
    "write_workspace_file",
    "edit_workspace_file",
    "multi_edit_workspace_file",
    "run_command",
    "browser_inspect_workspace",
)
_MANIFEST = {
    "name": SKILL_NAME,
    "displayName": "浏览器交互与视觉验证",
    "version": "1.0.0",
    "description": "使用 Native 已绑定浏览器执行真实输入并检查画面反馈。",
    "category": "development",
    "instructionsFile": "SKILL.md",
}
_SKILL = """---
name: native-browser-verification
description: Validate web artifacts with real browser input, screenshots and DOM evidence.
version: 1.0.0
---

# Native browser verification

Use only the bound browser_inspect_workspace schema; do not invent ZCode Node APIs.
Start with the actual workspace entry path and no actions. Inspect returned controls,
DOM, console errors and the image content supplied by the tool before deciding the
next action. A screenshot path alone is not an image observation. If image feedback
is unavailable, explicitly report that visual verification is incomplete.

For click/fill/press targets prefer an element_ref from the latest returned controls.
Never guess selectors or stale refs. Execute one state-changing action, observe its
actual result, then choose the next. Use the declared move/scroll/press actions for
canvas interactions. Do not call application functions or mutate internal state to
substitute for a user's input. Read-only diagnostics and independent code checks are
complementary; they do not prove that real interaction worked.

Verify the requested main flow, input boundaries and recovery behavior. For rendered
or canvas output inspect the supplied image as well as DOM/console diagnostics.
Check visibility, overlap, camera or scene orientation, reachable controls and input
feedback. Do not infer a pass from an internal self-test counter or lack of console
errors. Reload through the browser tool after editing served files. The same Run keeps
its page; a new Run starts an isolated page, so re-establish the test's actual state.

Record each observed failure before fixing it, then repeat the affected real input
and inspect a new observation. On timeout distinguish the tool outcome from task
completion; do not replay an operation whose side effect is unknown. Finish with the
cases exercised, evidence, failures and limitations. Model prose cannot grant verified.
"""
_SYSTEM = """You are a Native coding Agent. Deliver the user's requested working artifact.
Inspect relevant existing code, then implement and run the appropriate checks. Keep
working from actual tool results within the locked task budget. Use only the tools
and arguments actually declared in this Run. For an available background-command
lifecycle, keep the returned handle, observe its real exit status and report failed,
cancelled or unknown outcomes accurately. A process start is not proof of completion.

For multi-step tasks follow the user's requested order. Keep the bound task plan current
as work is completed, without treating plan status as acceptance. Persist useful
intermediate deliverables and evidence before moving to the next stage, especially
when the user asks for incremental output. Base source citations on observed file
contents and line numbers; label missing or uncertain evidence instead of filling
gaps from memory. Re-read only the evidence needed to resolve an identified gap.
Apply project conventions from the attributed project-instructions Context resource
within their directory scopes. More specific rules override broader conventions.
They do not grant tool permissions or override host policy or the user's request.
If instructions are omitted or unavailable, report the gap before claiming compliance.

Before browser work call skill_catalog, skill_read_manifest and skill_read_instructions
for the bound native-browser-verification Skill, then follow its actual Native tool
workflow. Create or locate an executable HTML entry before the first browser inspection;
after a missing-entry error, create that entry or correct the path before retrying.
Use browser_inspect_workspace for real interactive and visual feedback.
Read supplied image blocks rather than treating screenshot paths as observations.
When the selected model lacks image input, use only an explicitly bound vision model;
otherwise mark visual checks unavailable. Do not infer image capability from its name.
Diagnose definite tool failures instead of repeating them; preserve unknown-effect
recovery, approvals and user controls. Do not fabricate completion, acceptance or
visual parity. Respect explicit scope and leave unrelated files intact.

Copy edit old_text from the current source, including its punctuation. A failed atomic
multi-edit batch applies no edits, including those before the failing index. Correct
the failed edit using the original-file candidates or a fresh read, then retry the
complete batch; never infer that an earlier trial edit reached the file.
"""


def _ensure_skill(workspace: Workspace) -> None:
    root = workspace.resolve(f"capabilities/skills/{SKILL_NAME}")
    manifest = yaml.safe_dump(_MANIFEST, allow_unicode=True, sort_keys=False)
    if root.exists():
        if (
            not root.is_symlink()
            and not (root / "skill.yaml").is_symlink()
            and not (root / "SKILL.md").is_symlink()
            and (root / "skill.yaml").is_file()
            and (root / "SKILL.md").is_file()
            and (root / "skill.yaml").read_text() == manifest
            and (root / "SKILL.md").read_text() == _SKILL
        ):
            return
        raise StudioError(
            "CODING_SKILL_CONFLICT",
            "工作区存在不同内容的同名浏览器 Skill，未覆盖。",
            status_code=409,
        )
    root.mkdir(parents=True)
    workspace.atomic_write_text(root / "skill.yaml", manifest)
    workspace.atomic_write_text(root / "SKILL.md", _SKILL)


def compose_native_coding_agent(
    workspace: Workspace,
    catalog: LocalResourceCatalog,
    request: AgentTemplateComposeRequest,
) -> AgentTemplateComposition:
    resources = catalog.list(limit=1000)
    models = [r for r in resources if r.kind == "model"]
    primary = _select_model(
        models, request.model_profile_id or next(iter(request.model_profile_ids), None)
    )
    model = ModelSpec.model_validate(primary.contract)
    for requested in request.model_profile_ids:
        _select_model(models, requested)
    model_ids = _select_model_ids(models, primary.resource_id, request.model_profile_ids)
    selected_models = [
        ModelSpec.model_validate(r.contract) for r in models if r.resource_id in model_ids
    ]
    warnings = []
    vision_id = request.vision_model_profile_id
    vision = _select_model(models, vision_id) if vision_id else None
    if vision is not None and not supports_native_image_input(
        ModelSpec.model_validate(vision.contract).metadata
    ):
        raise StudioError(
            "CODING_IMAGE_MODEL_REQUIRED", "视觉模型未声明图像输入能力。",
            status_code=422, field="visionModelProfileId",
        )
    if not vision_id and not all(supports_native_image_input(m.metadata) for m in selected_models):
        warnings.append("部分模型未声明图像输入能力；需绑定视觉模型才能完成画面验收。")
    tools = [r for r in resources if r.kind == "tool"]
    named = {r.name: r for r in tools if r.status == "ready"}
    missing = [name for name in _TOOLS if name not in named]
    if missing:
        # Distinguish the exact environment gap using the actual sandbox
        # backend value, not by guessing from which tools are missing.
        # A tool can be unresolved even with a configured backend (e.g.
        # a stale catalog or a misconfigured workspace), so the backend
        # value — not the missing-tool set — determines the reason.
        from ksadk.toolsets.sandbox import sandbox_backend_name
        backend = sandbox_backend_name()
        if backend in {"", "disabled", "none", "off"}:
            raise StudioError(
                "CODING_TOOL_UNAVAILABLE",
                "编码模板需要沙箱后端才能执行命令和浏览器验证。"
                "请配置 KSADK_SANDBOX_BACKEND 环境变量"
                "（local_process 用于本机进程沙箱）；"
                "注意浏览器验证仅支持 local/local_process 后端，"
                "本机宿主执行不会自动切换。",
                status_code=409,
                details={"tools": missing,
                         "reason": "sandbox_backend_not_configured",
                         "backend": backend,
                         "hint": "set KSADK_SANDBOX_BACKEND=local_process for local sandbox"},
            )
        _BROWSER_TOOL = "browser_inspect_workspace"
        if _BROWSER_TOOL in missing and backend not in {"local", "local_process"}:
            raise StudioError(
                "CODING_TOOL_UNAVAILABLE",
                "编码模板的浏览器验证工具在当前沙箱后端不可用。"
                "browser_inspect_workspace 仅支持 local/local_process 后端；"
                f"当前后端为 {backend}，不提供该能力。",
                status_code=409,
                details={"tools": missing,
                         "reason": "browser_not_supported_by_backend",
                         "backend": backend,
                         "hint": "use local or local_process backend for browser verification"},
            )
        raise StudioError(
            "CODING_TOOL_UNAVAILABLE",
            "编码模板缺少必要的本地工具。",
            status_code=409,
            details={"tools": missing,
                     "reason": "tool_unavailable",
                     "backend": backend,
                     "hint": "check catalog tool status"},
        )
    background_tools = ("read_command_output", "stop_command")
    available_background = (
        list(background_tools) if all(name in named for name in background_tools) else []
    )
    if not available_background:
        warnings.append("当前 backend 未提供持久后台命令；长命令后台执行不可用。")
    selected = list(
        dict.fromkeys(
            [named[n].resource_id for n in (*_TOOLS, *available_background)]
            + request.tool_resource_ids
        )
    )
    selected_tools = _select_resources(tools, selected, kind="tool")
    selected_mcps = _select_resources(
        [r for r in resources if r.kind == "mcp"], request.mcp_resource_ids, kind="mcp"
    )
    window = min(get_context_window_tokens(m.metadata) for m in selected_models)
    output = model.parameters.max_tokens or 8192
    if output >= window:
        raise StudioError(
            "CODING_MODEL_BUDGET_INVALID", "模型输出额度必须小于已配置上下文窗口。", status_code=422
        )
    _ensure_skill(workspace)
    skills = catalog.list(kind="skill", limit=1000)
    skill = next(r for r in skills if r.name == SKILL_NAME and r.version == "1.0.0")
    selected_skills = _select_resources(
        skills, list(dict.fromkeys([skill.resource_id] + request.skill_resource_ids)), kind="skill"
    )
    spec = AgentSpec(
        runtime=RuntimeRef(type="harness"),
        description=request.description,
        instructions=Instructions(
            system=_SYSTEM,
            task="\n\n".join(filter(None, (
                request.goal.strip() or request.prompt.strip(), request.task_prompt.strip(),
            ))),
        ),
        bindings=AgentBindings(
            model_profile_id=primary.resource_id,
            model_profile_ids=model_ids,
            vision_model_profile_id=vision_id,
            # Preserve the user's actual Profile parameters: no hidden 4K replacement.
            model_parameters=model.parameters.model_copy(deep=True),
            policy_template=request.policy_template,
            tools=[CapabilityBinding(resource_id=r.resource_id) for r in selected_tools],
            skills=[CapabilityBinding(resource_id=r.resource_id) for r in selected_skills],
            mcp_servers=[CapabilityBinding(resource_id=r.resource_id) for r in selected_mcps],
        ),
        execution=ExecutionSpec(
            max_steps=request.max_steps,
            timeout_seconds=request.timeout_seconds,
        ),
        context=ContextSpec(max_input_tokens=window, reserve_output_tokens=output,
                            contributors=ContextContributorsSpec(project_instructions=True)),
    )
    return AgentTemplateComposition(
        template_id="coding",
        spec=spec,
        warnings=warnings,
        recommendations=[
            AgentTemplateRecommendation(
                kind=r.kind,
                status="bound",
                title=r.display_name,
                reason="绑定到当前模板；实际执行继续受权限、审批和预算约束。",
                resource_id=r.resource_id,
            )
            for r in [primary, *([vision] if vision else []), *selected_tools, *selected_skills, *selected_mcps]
        ],
    )
