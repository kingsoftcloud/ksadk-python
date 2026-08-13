"""Agent templates and deterministic composition for Studio authoring."""

from __future__ import annotations

from collections.abc import Iterable

import yaml  # type: ignore[import-untyped]

from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    AgentTemplateComposeRequest,
    AgentTemplateComposition,
    AgentTemplateRecommendation,
    CapabilityBinding,
    ContextSpec,
    ExecutionSpec,
    Instructions,
    ModelParameters,
    ResourceDescriptor,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.workspace import Workspace

RESEARCH_SKILL_NAME = "deep-research-methodology"
RESEARCH_SKILL_VERSION = "1.0.0"

_RESEARCH_SKILL_MANIFEST = {
    "name": RESEARCH_SKILL_NAME,
    "displayName": "深度调研方法",
    "version": RESEARCH_SKILL_VERSION,
    "description": "用于问题拆解、来源检索、交叉验证、证据归档与引用输出的标准方法。",
    "category": "research",
    "instructionsFile": "SKILL.md",
}

_RESEARCH_SKILL_CONTENT = """---
name: Deep Research Methodology
description: A rigorous workflow for evidence-backed research and source synthesis.
version: 1.0.0
---

# Deep Research Methodology

Apply this workflow to every research request:

1. Clarify the research question, decision context, scope, time range, and exclusions.
2. Build a question tree before collecting evidence. Separate facts, hypotheses, and unknowns.
3. Search broadly, then prefer primary and authoritative sources over summaries.
4. Record every material claim in a source ledger with title, URL, publisher, date, and access time.
5. Cross-check important claims with at least two independent sources when possible.
6. Explicitly identify conflicting evidence, stale information, and confidence limits.
7. Synthesize findings around the user's decision instead of returning a source dump.
8. Cite sources using stable labels such as [S1], [S2], and include a source table.
9. Never invent a source, URL, statistic, quotation, or publication date.
10. When external research tools are unavailable, state the limitation before answering.
"""

_MCP_KEYWORDS = {
    "browser",
    "crawl",
    "fetch",
    "http",
    "research",
    "search",
    "url",
    "web",
}

_DEPTH_SETTINGS = {
    "focused": {"max_steps": 8, "timeout_seconds": 180, "max_tokens": 3072},
    "standard": {"max_steps": 16, "timeout_seconds": 360, "max_tokens": 4096},
    "deep": {"max_steps": 28, "timeout_seconds": 900, "max_tokens": 6144},
}

_OUTPUT_NAMES_ZH = {
    "brief": "决策简报",
    "report": "结构化研究报告",
    "evidence-table": "证据矩阵与结论",
}

_OUTPUT_NAMES_EN = {
    "brief": "decision brief",
    "report": "structured research report",
    "evidence-table": "evidence matrix and conclusions",
}


def list_agent_templates() -> list[dict]:
    return [
        {
            "id": "blank",
            "name": "空白 Agent",
            "description": "输入系统提示词，自主选择模型、Tool、MCP、Skill 与执行策略。",
            "category": "general",
            "recommended": True,
            "steps": ["定义 Agent", "绑定能力", "配置 Prompt", "检查并创建"],
        },
        {
            "id": "research",
            "name": "Research Agent",
            "description": "围绕一个研究目标规划检索、调用 MCP、验证来源并生成带引用的报告。",
            "category": "knowledge-work",
            "recommended": False,
            "steps": ["定义目标", "绑定能力", "生成 Prompt", "检查并创建"],
        },
    ]


def default_agent_spec(
    template: str,
    *,
    description: str = "",
) -> AgentSpec:
    if template == "blank":
        return AgentSpec(
            description=description,
            instructions=Instructions(system="你是一个可靠的企业智能助手。", task=""),
        )
    if template == "research":
        prompt = _research_instructions(
            AgentTemplateComposeRequest(
                goal="围绕用户输入的问题进行系统性深度调研并输出可核验结论",
            ),
            has_research_mcp=False,
        )
        return AgentSpec(
            description=description or "执行深度调研并输出带来源引用的研究报告。",
            instructions=prompt,
            execution=ExecutionSpec(
                strategy="plan-act-observe",
                max_steps=28,
                timeout_seconds=900,
            ),
        )
    raise StudioError(
        "AGENT_TEMPLATE_UNSUPPORTED",
        "不支持的 Agent 模板",
        status_code=422,
        field="template",
        details={"template": template},
    )


def ensure_research_skill(workspace: Workspace) -> None:
    root = workspace.resolve(f"capabilities/skills/{RESEARCH_SKILL_NAME}")
    manifest = root / "skill.yaml"
    instructions = root / "SKILL.md"
    if root.exists():
        if manifest.is_file() and instructions.is_file():
            return
        raise StudioError(
            "RESEARCH_SKILL_CONFLICT",
            "工作区存在不完整的同名 Research Skill",
            status_code=409,
            details={"path": workspace.relative(root)},
        )
    root.mkdir(parents=True)
    workspace.atomic_write_text(
        manifest,
        yaml.safe_dump(
            _RESEARCH_SKILL_MANIFEST,
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    workspace.atomic_write_text(instructions, _RESEARCH_SKILL_CONTENT)


def compose_blank_agent(
    workspace: Workspace,
    catalog: LocalResourceCatalog,
    request: AgentTemplateComposeRequest,
) -> AgentTemplateComposition:
    del workspace
    resources = catalog.list(limit=200)
    models = [item for item in resources if item.kind == "model"]
    tools = [item for item in resources if item.kind == "tool"]
    skills = [item for item in resources if item.kind == "skill"]
    mcps = [item for item in resources if item.kind == "mcp"]

    model = _select_model(models, request.model_profile_id)
    selected_model_ids = _select_model_ids(
        models, request.model_profile_id, request.model_profile_ids
    )
    selected_tools = _select_resources(
        tools,
        request.tool_resource_ids,
        kind="tool",
    )
    selected_skills = _select_resources(
        skills,
        request.skill_resource_ids,
        kind="skill",
    )
    selected_mcps = _select_resources(
        mcps,
        request.mcp_resource_ids,
        kind="mcp",
    )
    system_prompt = request.prompt.strip() or request.goal.strip()
    task_prompt = request.task_prompt.strip() or (
        "理解用户目标并给出准确、可执行的回答。调用 Tool 前检查输入，"
        "对外部操作遵守权限策略；信息不足时说明假设并请求必要补充。"
    )
    description = request.description.strip() or system_prompt[:160]
    bindings = AgentBindings(
        model_profile_id=model.resource_id,
        model_profile_ids=selected_model_ids,
        model_parameters=ModelParameters(
            temperature=0.2,
            max_tokens=4096,
        ),
        policy_template=request.policy_template,
        tools=[CapabilityBinding(resource_id=item.resource_id) for item in selected_tools],
        mcp_servers=[CapabilityBinding(resource_id=item.resource_id) for item in selected_mcps],
        skills=[CapabilityBinding(resource_id=item.resource_id) for item in selected_skills],
    )
    spec = AgentSpec(
        description=description,
        instructions=Instructions(system=system_prompt, task=task_prompt),
        bindings=bindings,
        execution=ExecutionSpec(
            strategy=request.execution_strategy,
            max_steps=request.max_steps,
            timeout_seconds=request.timeout_seconds,
        ),
        context=ContextSpec(
            max_input_tokens=65536,
            reserve_output_tokens=8192,
        ),
    )
    recommendations = [
        AgentTemplateRecommendation(
            kind="model",
            status="bound",
            title=model.display_name,
            reason="负责理解用户请求、规划执行并生成回答。",
            resource_id=model.resource_id,
        )
    ]
    recommendations.extend(
        _bound_recommendations(
            selected_tools,
            reason="按用户选择绑定到 Agent，可用范围由权限策略控制。",
        )
    )
    recommendations.extend(
        _bound_recommendations(
            selected_mcps,
            reason="按用户选择连接外部 MCP Server。",
        )
    )
    recommendations.extend(
        _bound_recommendations(
            selected_skills,
            reason="按用户选择注入版本化 Skill 指令。",
        )
    )
    return AgentTemplateComposition(
        template_id="blank",
        spec=spec,
        recommendations=recommendations,
        warnings=[],
    )


def compose_research_agent(
    workspace: Workspace,
    catalog: LocalResourceCatalog,
    request: AgentTemplateComposeRequest,
) -> AgentTemplateComposition:
    ensure_research_skill(workspace)
    resources = catalog.list(limit=200)
    models = [item for item in resources if item.kind == "model"]
    tools = [item for item in resources if item.kind == "tool"]
    skills = [item for item in resources if item.kind == "skill"]
    mcps = [item for item in resources if item.kind == "mcp"]

    model = _select_model(models, request.model_profile_id)
    selected_model_ids = _select_model_ids(
        models, request.model_profile_id, request.model_profile_ids
    )
    research_skill = next(
        (
            item
            for item in skills
            if item.name == RESEARCH_SKILL_NAME and item.version == RESEARCH_SKILL_VERSION
        ),
        None,
    )
    if research_skill is None or research_skill.status != "ready":
        raise StudioError(
            "RESEARCH_SKILL_UNAVAILABLE",
            "Research 模板无法解析内置调研 Skill",
            status_code=409,
        )

    selected_skills = _select_resources(
        skills,
        [research_skill.resource_id, *request.skill_resource_ids],
        kind="skill",
    )
    compatible_mcps = [item for item in mcps if _is_research_mcp(item)]
    requested_mcp_ids = request.mcp_resource_ids or (
        [item.resource_id for item in compatible_mcps[:1]] if request.auto_bind_mcp else []
    )
    selected_mcps = _select_resources(
        mcps,
        requested_mcp_ids,
        kind="mcp",
    )
    default_tool_ids = [
        item.resource_id
        for item in tools
        if item.name
        in {
            "list_workspace_files",
            "search_workspace_files",
            "read_workspace_file",
        }
        and item.status == "ready"
    ]
    requested_tool_ids = request.tool_resource_ids or (
        default_tool_ids if request.auto_bind_tools else []
    )
    selected_tools = _select_resources(
        tools,
        requested_tool_ids,
        kind="tool",
    )

    settings = _DEPTH_SETTINGS[request.depth]
    bindings = AgentBindings(
        model_profile_id=model.resource_id,
        model_profile_ids=selected_model_ids,
        model_parameters=ModelParameters(
            temperature=0.2,
            max_tokens=settings["max_tokens"],
        ),
        policy_template=request.policy_template,
        tools=[CapabilityBinding(resource_id=item.resource_id) for item in selected_tools],
        mcp_servers=[CapabilityBinding(resource_id=item.resource_id) for item in selected_mcps],
        skills=[CapabilityBinding(resource_id=item.resource_id) for item in selected_skills],
    )
    has_research_mcp = bool(selected_mcps)
    instructions = _research_instructions(
        request,
        has_research_mcp=has_research_mcp,
    )
    spec = AgentSpec(
        description=request.description.strip()
        or f"围绕“{_research_goal(request)[:160]}”执行深度调研并形成可核验结论。",
        instructions=instructions,
        bindings=bindings,
        execution=ExecutionSpec(
            strategy="plan-act-observe",
            max_steps=settings["max_steps"],
            timeout_seconds=settings["timeout_seconds"],
        ),
        context=ContextSpec(
            max_input_tokens=65536,
            reserve_output_tokens=8192,
        ),
    )

    recommendations = [
        AgentTemplateRecommendation(
            kind="model",
            status="bound",
            title=model.display_name,
            reason="负责问题拆解、证据综合与报告生成。",
            resource_id=model.resource_id,
        ),
        AgentTemplateRecommendation(
            kind="skill",
            status="bound",
            title=research_skill.display_name,
            reason="注入问题树、来源分级、交叉验证和引用账本方法。",
            resource_id=research_skill.resource_id,
        ),
    ]
    recommendations.extend(
        AgentTemplateRecommendation(
            kind="tool",
            status="bound",
            title=item.display_name,
            reason="用于读取和检索当前工作区中的研究资料。",
            resource_id=item.resource_id,
        )
        for item in selected_tools
    )
    warnings: list[str] = []
    if selected_mcps:
        recommendations.extend(
            AgentTemplateRecommendation(
                kind="mcp",
                status="bound",
                title=item.display_name,
                reason="提供外部来源搜索、抓取或浏览能力。",
                resource_id=item.resource_id,
            )
            for item in selected_mcps
        )
    else:
        warning = (
            "当前没有已探测且包含搜索或抓取 Tool 的 MCP。Agent 仍可基于用户输入和"
            "工作区资料调研，但无法主动检索外部来源。"
        )
        warnings.append(warning)
        recommendations.append(
            AgentTemplateRecommendation(
                kind="mcp",
                status="missing",
                title="Web Research MCP",
                reason=warning,
            )
        )
    return AgentTemplateComposition(
        template_id="research",
        spec=spec,
        recommendations=recommendations,
        warnings=warnings,
    )


def _select_model(
    models: list[ResourceDescriptor],
    resource_id: str | None,
) -> ResourceDescriptor:
    if resource_id:
        selected = next(
            (item for item in models if item.resource_id == resource_id),
            None,
        )
        if selected is None:
            raise StudioError(
                "RESOURCE_NOT_FOUND",
                "选择的 Model Profile 不存在",
                status_code=404,
                details={"resourceId": resource_id},
            )
        if selected.status not in {"ready", "missing-secret"}:
            raise StudioError(
                "RESOURCE_NOT_READY",
                "选择的 Model Profile 当前不可用",
                status_code=409,
                details={"resourceId": resource_id, "status": selected.status},
            )
        return selected
    selected = next(
        (item for item in models if item.status in {"ready", "missing-secret"}),
        None,
    )
    if selected is None:
        raise StudioError(
            "AGENT_MODEL_REQUIRED",
            "Agent 需要一个可用的 Model Profile",
            status_code=409,
        )
    return selected


def _select_model_ids(
    models: list[ResourceDescriptor],
    primary_id: str | None,
    requested_ids: list[str] | None,
) -> list[str]:
    """Resolve the full set of bound model profile ids, deduped, primary first."""
    valid = [item for item in models if item.status in {"ready", "missing-secret"}]
    valid_ids = {item.resource_id for item in valid}
    ordered: list[str] = []
    if primary_id and primary_id in valid_ids:
        ordered.append(primary_id)
    for rid in requested_ids or []:
        if rid in valid_ids and rid not in ordered:
            ordered.append(rid)
    return ordered



def _bound_recommendations(
    resources: list[ResourceDescriptor],
    *,
    reason: str,
) -> list[AgentTemplateRecommendation]:
    return [
        AgentTemplateRecommendation(
            kind=item.kind,
            status="bound",
            title=item.display_name,
            reason=reason,
            resource_id=item.resource_id,
        )
        for item in resources
    ]


def _select_resources(
    resources: list[ResourceDescriptor],
    resource_ids: Iterable[str],
    *,
    kind: str,
) -> list[ResourceDescriptor]:
    selected: list[ResourceDescriptor] = []
    seen: set[str] = set()
    by_id = {item.resource_id: item for item in resources}
    for resource_id in resource_ids:
        if resource_id in seen:
            continue
        seen.add(resource_id)
        descriptor = by_id.get(resource_id)
        if descriptor is None:
            raise StudioError(
                "RESOURCE_NOT_FOUND",
                "模板引用的资源不存在",
                status_code=404,
                details={"resourceId": resource_id},
            )
        if descriptor.kind != kind:
            raise StudioError(
                "RESOURCE_KIND_INVALID",
                "模板资源类型不匹配",
                status_code=422,
                details={"resourceId": resource_id, "expectedKind": kind},
            )
        if descriptor.status != "ready":
            raise StudioError(
                "RESOURCE_NOT_READY",
                "模板不能绑定不可用资源",
                status_code=409,
                details={"resourceId": resource_id, "status": descriptor.status},
            )
        selected.append(descriptor)
    return selected


def _is_research_mcp(resource: ResourceDescriptor) -> bool:
    if resource.status != "ready":
        return False
    discovered_tools = resource.contract.get("discoveredTools") or []
    if not discovered_tools:
        return False
    searchable = " ".join(
        [
            resource.name,
            resource.display_name,
            resource.description,
            *[str(tool.get("name") or "") for tool in discovered_tools],
            *[str(tool.get("description") or "") for tool in discovered_tools],
        ]
    ).lower()
    return any(keyword in searchable for keyword in _MCP_KEYWORDS)


def _research_instructions(
    request: AgentTemplateComposeRequest,
    *,
    has_research_mcp: bool,
) -> Instructions:
    research_goal = _research_goal(request)
    if request.language == "en-US":
        output_name = _OUTPUT_NAMES_EN[request.output_format]
        role = f"You are Research Agent, a rigorous research specialist serving {request.audience}."
        system = f"""{role}

Research objective:
{research_goal}

Operating principles:
1. Clarify scope and assumptions before collecting evidence.
2. Create a question tree and an explicit research plan.
3. Prefer primary, authoritative, and recent sources.
4. Cross-check material claims and distinguish facts from inference.
5. Keep a source ledger and cite every externally verifiable claim.
6. Never invent a source, URL, quotation, statistic, or publication date.
7. Surface conflicting evidence, uncertainty, and information gaps.
8. Produce a {output_name} for the user's decision context.
"""
        if not has_research_mcp:
            system += (
                "\nExternal research MCP is not currently bound. Use only supplied or "
                "workspace evidence and state this limitation before the answer.\n"
            )
        task = """For each request, execute this contract:
- Restate the research question, scope, and exclusions.
- Present the plan before long-running research.
- Gather and rank evidence by source quality and recency.
- Synthesize findings into an executive summary, key findings, evidence table,
  disagreements or risks, recommendations, and a source list.
- Use stable citations such as [S1] and map each citation to a real source.
- End with confidence and unresolved questions.
"""
        return Instructions(system=system.strip(), task=task.strip())

    output_name = _OUTPUT_NAMES_ZH[request.output_format]
    system = f"""你是 Research Agent，一名为{request.audience}服务的严谨深度调研专家。

调研目标：
{research_goal}

工作原则：
1. 收集证据前先明确范围、时间窗口、假设和排除项。
2. 建立问题树和可执行调研计划，再开始调用工具。
3. 优先使用一手、权威且时效性匹配的来源。
4. 对重要结论交叉验证，清楚区分事实、推断与未知。
5. 维护来源账本，为每个可外部核验的关键主张提供引用。
6. 不得虚构来源、URL、引文、统计数据或发布日期。
7. 主动呈现冲突证据、不确定性和信息缺口。
8. 围绕用户的决策场景输出{output_name}。
"""
    if not has_research_mcp:
        system += (
            "\n当前未绑定外部调研 MCP。只能使用用户提供内容和工作区资料，"
            "回答前必须明确说明这一限制。\n"
        )
    task = """每次请求都必须执行以下任务契约：
- 复述研究问题、范围和排除项。
- 在长时间调研前先展示计划。
- 按来源质量和时效性收集、筛选和排序证据。
- 输出执行摘要、关键发现、证据表、冲突或风险、建议和来源列表。
- 使用 [S1]、[S2] 等稳定引用，并映射到真实来源。
- 结尾给出置信度和仍待解决的问题。
"""
    return Instructions(system=system.strip(), task=task.strip())


def _research_goal(request: AgentTemplateComposeRequest) -> str:
    return request.goal.strip() or request.prompt.strip()
