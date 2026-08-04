function currentWizardPayload() {
  const prompt = $("agentPrompt").value.trim();
  return {
    prompt,
    goal: state.wizard.template === "research" ? prompt : "",
    description: $("agentDescription").value.trim(),
    taskPrompt: $("generatedTaskPrompt").value.trim(),
    audience: $("researchAudience").value.trim() || "技术与业务决策者",
    language: $("researchLanguage").value,
    depth: state.wizard.depth,
    outputFormat: $("researchFormat").value,
    modelProfileId: $("agentModel").value || null,
    toolResourceIds: state.wizard.selectedToolIds,
    skillResourceIds: state.wizard.selectedSkillIds,
    mcpResourceIds: state.wizard.selectedMcpIds,
    policyTemplate: state.wizard.policyTemplate,
    executionStrategy: state.wizard.template === "research" ? "plan-act-observe" : "direct",
    maxSteps: state.wizard.template === "research" ? 28 : 12,
    timeoutSeconds: state.wizard.template === "research" ? 900 : 120,
    autoBindTools: state.wizard.autoBindTools,
    autoBindMcp: state.wizard.autoBindMcp
  };
}

async function composeAgent({ preservePrompt = true } = {}) {
  const requestSequence = ++state.composeSequence;
  state.wizard.composing = true;
  $("promptStatus").innerHTML = '<span class="status-dot info"></span><span>正在根据模板与能力生成 Agent 配置</span>';
  try {
    const previousSystem = $("generatedSystemPrompt").value;
    const previousTask = $("generatedTaskPrompt").value;
    const composition = await api(`/agent-templates/${state.wizard.template}:compose`, {
      method: "POST",
      body: currentWizardPayload()
    });
    if (requestSequence !== state.composeSequence) return state.wizard.composition;
    await refreshCatalog();
    if (requestSequence !== state.composeSequence) return state.wizard.composition;
    state.wizard.composition = composition;
    if (state.wizard.runtime === "codex") {
      composition.spec.bindings.tools = [];
      composition.spec.bindings.skills = [];
      composition.spec.bindings.mcpServers = [];
    }
    state.wizard.selectedToolIds = composition.spec.bindings.tools.map(item => item.resourceId);
    state.wizard.selectedSkillIds = composition.spec.bindings.skills.map(item => item.resourceId);
    state.wizard.selectedMcpIds = composition.spec.bindings.mcpServers.map(item => item.resourceId);
    renderWizardCapabilities();
    if (!preservePrompt || !previousSystem.trim()) {
      $("generatedSystemPrompt").value = composition.spec.instructions.system;
    } else {
      $("generatedSystemPrompt").value = previousSystem;
    }
    if (!preservePrompt || !previousTask.trim()) {
      $("generatedTaskPrompt").value = composition.spec.instructions.task;
    } else {
      $("generatedTaskPrompt").value = previousTask;
    }
    $("promptStatus").innerHTML = '<span class="status-dot success"></span><span>Agent 配置已根据当前选择生成</span>';
    renderWizardSummary();
    return composition;
  } finally {
    if (requestSequence === state.composeSequence) state.wizard.composing = false;
  }
}

function resetWizard() {
  state.composeSequence += 1;
  state.wizard = {
    step: 1,
    template: "blank",
    runtime: "codex",
    depth: "deep",
    composition: null,
    selectedToolIds: [],
    selectedSkillIds: [],
    selectedMcpIds: [],
    policyTemplate: "strict",
    autoBindTools: false,
    autoBindMcp: false,
    composing: false
  };
  $("createAgentForm").reset();
  $("newAgentName").value = "New Agent";
  $("newAgentId").value = uniqueAgentId("new-agent");
  $("agentDescription").value = "";
  $("agentRuntime").value = "codex";
  $("agentPrompt").value = "";
  $("researchAudience").value = "产品与技术负责人";
  $("researchLanguage").value = "zh-CN";
  $("researchFormat").value = "report";
  $("buildAfterCreate").checked = true;
  $("generatedSystemPrompt").value = "";
  $("generatedTaskPrompt").value = "";
  $("createError").hidden = true;
  document.querySelectorAll("#researchDepth .choice-card").forEach(card => {
    card.classList.toggle("selected", card.dataset.value === "deep");
  });
  document.querySelectorAll("#policyTemplate button").forEach(button => {
    button.classList.toggle("selected", button.dataset.policy === "strict");
  });
  $("policyDescription").textContent = policyCopy("strict").description;
  updateTemplateUi();
  populateModelSelect();
  setWizardStep(1);
  updatePromptCounter();
  renderWizardSummary();
}

function runtimeRef(agentId, runtimeType) {
  if (runtimeType === "codex") {
    return { type: "codex", version: "0.144.4" };
  }
  return {
    type: runtimeType,
    projectPath: `agents/${agentId}/source`,
    entryPoint: "agent.py",
    agentVariable: runtimeType === "langgraph" ? "graph" : "root_agent",
    detection: "declared"
  };
}

function updateRuntimeUi() {
  state.wizard.runtime = $("agentRuntime").value || "codex";
  const descriptions = {
    codex: "由 CodexRuntimeAdapter 直接运行，支持星流 Proxy；当前只绑定模型，ksadk Tool、MCP 与 Skill 不会伪装为已兼容。",
    adk: "生成 Google ADK 源码，由 ADKRuntimeAdapter 执行。",
    langgraph: "生成带 MemorySaver 的 LangGraph 源码，由 LangGraphRuntimeAdapter 执行。"
  };
  if (state.wizard.runtime === "codex") {
    state.wizard.selectedToolIds = [];
    state.wizard.selectedSkillIds = [];
    state.wizard.selectedMcpIds = [];
    state.wizard.autoBindTools = false;
    state.wizard.autoBindMcp = false;
  }
  $("agentRuntimeHelper").textContent = descriptions[state.wizard.runtime];
  if (state.wizard.composition) renderWizardCapabilities();
  renderWizardSummary();
}

function uniqueAgentId(base) {
  const ids = new Set(state.agents.map(item => item.metadata.id));
  if (!ids.has(base)) return base;
  let index = 2;
  while (ids.has(`${base}-${index}`)) index += 1;
  return `${base}-${index}`;
}

function updateTemplateUi() {
  const research = state.wizard.template === "research";
  document.querySelectorAll("#agentTemplatePicker [data-template]").forEach(card => {
    card.classList.toggle("selected", card.dataset.template === state.wizard.template);
  });
  $("researchTemplateOptions").hidden = !research;
  $("agentPromptLabel").textContent = research ? "调研目标" : "系统提示词";
  $("agentPromptHelper").textContent = research
    ? "包含决策背景、调研范围和希望解决的问题"
    : "写清角色、目标、工作边界和回答方式";
  $("agentPrompt").placeholder = research
    ? "例如：调研企业级 Agent 编排平台的核心能力、主流技术路线、代表产品和落地风险，为一期架构选型提供依据。"
    : "例如：你是一名企业技术支持助手。先识别问题类型，再结合知识库给出准确、可执行的处理步骤；信息不足时先提问，不要编造事实。";
  $("agentPrompt").minLength = research ? 8 : 4;
  $("createTemplateEyebrow").textContent = research ? "Research Template" : "Agent Builder";
  $("createPageDescription").textContent = research
    ? "输入调研目标，按需调整预置 Skill、Tool 与 MCP，再生成可编辑的研究契约。"
    : "从系统提示词开始，按需组合模型、Tool、MCP 与 Skill。";
  $("skillRecommendation").hidden = !research;
  renderWizardSummary();
}

function selectAgentTemplate(template) {
  if (!["blank", "research"].includes(template) || template === state.wizard.template) return;
  const previous = state.wizard.template;
  state.composeSequence += 1;
  state.wizard.template = template;
  state.wizard.composition = null;
  state.wizard.selectedToolIds = [];
  state.wizard.selectedSkillIds = [];
  state.wizard.selectedMcpIds = [];
  state.wizard.autoBindTools = template === "research";
  state.wizard.autoBindMcp = template === "research";
  $("generatedSystemPrompt").value = "";
  $("generatedTaskPrompt").value = "";
  $("promptStatus").innerHTML = '<span class="status-dot info"></span><span>进入此步骤后生成 Agent 配置</span>';

  const previousName = previous === "research" ? "Research Agent" : "New Agent";
  const previousIdPrefix = previous === "research" ? "research-agent" : "new-agent";
  if (!$("newAgentName").value.trim() || $("newAgentName").value === previousName) {
    $("newAgentName").value = template === "research" ? "Research Agent" : "New Agent";
  }
  if (
    !$("newAgentId").value.trim()
    || isGeneratedAgentId($("newAgentId").value.trim(), previousIdPrefix)
  ) {
    $("newAgentId").value = uniqueAgentId(template === "research" ? "research-agent" : "new-agent");
  }
  updateTemplateUi();
}

function isGeneratedAgentId(value, prefix) {
  return value === prefix || new RegExp(`^${prefix}-\\d+$`).test(value);
}

function openCreate() {
  state.editingAgentId = null;
  state.authoringConversation = [];
  state.authoringProposal = null;
  state.importInspection = null;
  state.projectInspection = null;
  resetWizard();
  renderAuthoringTranscript();
  $("authoringConversationInput").value = "";
  $("authoringProposalJson").textContent = "完成一轮或多轮对话后，这里会出现可编辑的 Draft Patch。";
  $("authoringProposalDot").className = "status-dot";
  $("confirmConversationAgent").disabled = true;
  $("agentImportInspection").textContent = "选择文件并检查后显示解析结果、警告和 RuntimeRef。";
  $("agentImportCommit").disabled = true;
  $("projectInspection").textContent = "输入本地项目路径后显示 FrameworkDetector 证据。";
  $("projectCommit").disabled = true;
  switchView("create", { parent: "Agent", title: "创建 Agent" });
  $("authoringModeTabs").hidden = false;
  setAuthoringMode("quick");
  $("newAgentName").focus();
}

function setAuthoringMode(mode) {
  if (!["quick", "conversation", "import", "project"].includes(mode)) return;
  state.authoringMode = mode;
  document.querySelectorAll("[data-authoring-mode]").forEach(button => {
    button.classList.toggle("active", button.dataset.authoringMode === mode);
  });
  $("quickAgentEditor").hidden = true;
  document.querySelector(".wizard-layout").hidden = mode !== "quick";
  $("authoringConversationPanel").hidden = mode !== "conversation";
  $("authoringImportPanel").hidden = mode !== "import";
  $("authoringProjectPanel").hidden = mode !== "project";
  if (mode === "conversation") populateConversationModels();
  injectIcons($("view-create"));
}

function populateConversationModels() {
  const models = state.catalog.model;
  const current = $("authoringConversationModel").value;
  $("authoringConversationModel").innerHTML = models.map(item => `
    <option value="${escapeHtml(item.resourceId)}">${escapeHtml(item.displayName)} · ${escapeHtml(item.contract?.model || item.name)}</option>
  `).join("");
  if (models.some(item => item.resourceId === current)) {
    $("authoringConversationModel").value = current;
  }
}

function renderAuthoringTranscript() {
  const messages = state.authoringConversation.filter(item => item.role === "user");
  $("authoringTranscript").innerHTML = messages.length
    ? messages.map((item, index) => `<article class="authoring-message"><span>第 ${index + 1} 轮</span><p>${escapeHtml(item.content)}</p></article>`).join("")
    : '<div class="trace-stage-empty compact"><p>说明 Agent 的职责、边界、Runtime 和期望能力。</p></div>';
}

async function composeConversationAgent() {
  const input = $("authoringConversationInput").value.trim();
  const modelProfileId = $("authoringConversationModel").value;
  if (!input || !modelProfileId) {
    showToast("缺少构建信息", "请输入需求并选择用于构建的模型。", "error");
    return;
  }
  const button = $("authoringConversationSend");
  state.authoringConversation.push({ role: "user", content: input });
  $("authoringConversationInput").value = "";
  renderAuthoringTranscript();
  setButtonLoading(button, true, "正在生成");
  try {
    const result = await api("/authoring/conversations:compose", {
      method: "POST",
      body: { messages: state.authoringConversation, modelProfileId }
    });
    state.authoringProposal = result.proposal;
    state.authoringConversation.push({
      role: "assistant",
      content: JSON.stringify(result.proposal)
    });
    $("proposalName").value = result.proposal.name;
    $("proposalSlug").value = result.proposal.slug;
    $("proposalRuntime").value = result.proposal.runtimeType;
    $("proposalPrompt").value = result.proposal.instructions.system;
    $("authoringProposalJson").textContent = JSON.stringify(result.proposal, null, 2);
    $("authoringProposalDot").className = "status-dot success";
    $("confirmConversationAgent").disabled = false;
  } catch (error) {
    state.authoringConversation.pop();
    showToast("对话构建失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}

async function finishAuthoredAgent(created, message) {
  await refreshAgents();
  state.current = await api(`/agents/${encodeURIComponent(created.metadata.id)}`);
  state.build = currentSuccessfulBuild(state.current);
  renderGlobalContext();
  showToast("Agent Revision 已创建", message);
  await openAgentDetail(created.metadata.id);
}

async function confirmConversationAgent(event) {
  event.preventDefault();
  if (!state.authoringProposal) return;
  const proposal = state.authoringProposal;
  const created = await api("/authoring/quick", {
    method: "POST",
    body: {
      name: $("proposalName").value.trim(),
      slug: $("proposalSlug").value.trim(),
      runtimeType: $("proposalRuntime").value,
      description: proposal.description || "",
      spec: {
        instructions: {
          system: $("proposalPrompt").value.trim(),
          task: proposal.instructions?.task || ""
        },
        bindings: {
          modelProfileId: $("authoringConversationModel").value || null,
          modelProfileIds: $("authoringConversationModel").value
            ? [$("authoringConversationModel").value]
            : []
        }
      }
    }
  });
  await finishAuthoredAgent(created, "模型生成的 Draft Patch 已经用户确认并写入工作区。");
}

async function inspectAgentImport(event) {
  event.preventDefault();
  const file = $("agentImportFile").files?.[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  state.importInspection = await api("/authoring/imports:inspect", {
    method: "POST",
    body
  });
  $("agentImportName").value = state.importInspection.displayName;
  $("agentImportSlug").value = state.importInspection.displayName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "imported-agent";
  $("agentImportInspection").textContent = JSON.stringify(state.importInspection, null, 2);
  $("agentImportCommit").disabled = false;
}

async function commitAgentImport(event) {
  event.preventDefault();
  if (!state.importInspection) return;
  const created = await api(`/authoring/imports/${encodeURIComponent(state.importInspection.inspectionToken)}:commit`, {
    method: "POST",
    body: {
      name: $("agentImportName").value.trim(),
      slug: $("agentImportSlug").value.trim()
    }
  });
  state.importInspection = null;
  await finishAuthoredAgent(created, "导入检查已确认，canonical Agent 已写入工作区。");
}

async function inspectAgentProject(event) {
  event.preventDefault();
  state.projectInspection = await api("/authoring/projects:inspect", {
    method: "POST",
    body: { path: $("projectInspectPath").value.trim() }
  });
  const fallback = state.projectInspection.name || "Detected Agent";
  $("projectAgentName").value = fallback;
  $("projectAgentSlug").value = fallback.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "detected-agent";
  $("projectInspection").textContent = JSON.stringify(state.projectInspection, null, 2);
  $("projectCommit").disabled = false;
}

async function commitAgentProject(event) {
  event.preventDefault();
  if (!state.projectInspection) return;
  const created = await api(`/authoring/projects/${encodeURIComponent(state.projectInspection.inspectionToken)}:commit`, {
    method: "POST",
    body: {
      name: $("projectAgentName").value.trim(),
      slug: $("projectAgentSlug").value.trim(),
      modelProfileId: state.catalog.model[0]?.resourceId || null
    }
  });
  state.projectInspection = null;
  await finishAuthoredAgent(created, "FrameworkDetector 证据已确认，原项目源码未被重写。");
}

function prepareQuickCreate(editingDetail = null) {
  const editing = Boolean(editingDetail);
  const reference = editingDetail?.draft || state.current?.draft || state.agents[0] || null;
  const runtimeType = reference?.spec?.runtime?.type
    || reference?.metadata?.labels?.["agentkit.ksyun.com/framework"]
    || "codex";
  const currentModelId = reference?.spec?.bindings?.modelProfileId || "";
  const currentModelName = reference?.metadata?.labels?.["agentkit.ksyun.com/model"] || "glm-5.1";
  const models = state.catalog.model;
  const configuredIds = reference?.spec?.bindings?.modelProfileIds?.length
    ? reference.spec.bindings.modelProfileIds
    : currentModelId ? [currentModelId] : [];
  const inferredCurrent = models.find(item => (
    item.contract?.model || item.name
  ) === currentModelName)?.resourceId;
  const selectedIds = configuredIds.length
    ? configuredIds
    : inferredCurrent ? [inferredCurrent] : models[0] ? [models[0].resourceId] : [];
  $("quickAgentModels").innerHTML = models.length
    ? models.map(item => `
      <label class="quick-model-option" title="${escapeHtml(item.description || item.displayName)}">
        <input type="checkbox" data-quick-model-id="${escapeHtml(item.resourceId)}" ${selectedIds.includes(item.resourceId) ? "checked" : ""}>
        <span><strong>${escapeHtml(item.displayName)}</strong><small>${escapeHtml(item.contract?.model || item.name || "")}</small></span>
      </label>
    `).join("")
    : '<div class="session-empty">当前模型服务没有返回可绑定模型</div>';
  syncQuickModelSelect(currentModelId || selectedIds[0] || "", currentModelName);
  if (editing) {
    const draft = editingDetail.draft;
    $("quickAgentName").value = draft.metadata.name;
    $("quickAgentId").value = draft.metadata.id;
    $("quickAgentPrompt").value = draft.spec.instructions.system;
    $("quickAgentTask").value = "";
  } else if (state.agents.length) {
    const next = state.agents.length + 1;
    $("quickAgentName").value = `Assistant ${next}`;
    $("quickAgentId").value = uniqueAgentId("assistant");
    $("quickAgentPrompt").value = "你是一个可靠的工作助手。先理解用户目标和当前上下文，再给出准确、可执行且边界清晰的结果；信息不足时先说明缺口。";
  } else {
    $("quickAgentName").value = "Review Helper";
    $("quickAgentId").value = "review-helper";
    $("quickAgentPrompt").value = "你是代码审查助手。请先理解用户目标和工作区代码，只报告能够定位且可复现的问题，并给出最小修复建议。";
  }
  $("quickAgentRuntime").value = runtimeType;
  $("quickAgentRuntime").disabled = editing;
  $("quickRuntimeTitle").textContent = `${runtimeType === "adk" ? "ADK" : runtimeType === "langgraph" ? "LangGraph" : "Codex"}RuntimeAdapter`;
  $("quickAgentName").readOnly = editing;
  $("quickAgentId").readOnly = editing;
  $("createPageTitle").textContent = editing ? "编辑 Agent" : "创建 Agent";
  $("createPageDescription").textContent = editing
    ? "修改系统提示词与模型绑定；本地标识保持不变，避免破坏已有引用。"
    : "从系统提示词开始，按需组合模型、Tool、MCP 与 Skill。";
  $("quickCreateHeading").textContent = editing
    ? `编辑 ${reference.metadata.id}`
    : "描述角色，保存后即可构建与对话";
  $("quickCreateDescription").textContent = editing
    ? "保存会直接回写该 Agent 的 agentengine.yaml；旧构建会标记为过期。"
    : "每个 Agent 维护一份 YAML 配置源；构建会生成不可变审计制品，自定义 Tool 可在后续 Bundle 中加入。";
  $("quickSuggestedTaskField").hidden = editing;
  $("quickBuildAfterCreate").checked = !editing;
  $("quickBuildActionTitle").textContent = editing ? "保存后立即重新构建" : "保存后立即构建";
  $("quickBuildActionDescription").textContent = editing
    ? "新构建完成后直接进入会话工作台"
    : "构建成功后直接进入会话工作台";
  $("quickCreateSubmit").querySelector("span").textContent = editing ? "保存修改" : "保存并构建";
  renderQuickManifestPreview();
  injectIcons($("quickAgentEditor"));
}

async function openEditAgent(agentId) {
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  state.current = detail;
  state.build = currentSuccessfulBuild(detail);
  renderGlobalContext();
  state.editingAgentId = agentId;
  switchView("create", { parent: "Agent", title: "编辑 Agent" });
  $("authoringModeTabs").hidden = true;
  $("authoringConversationPanel").hidden = true;
  $("authoringImportPanel").hidden = true;
  $("authoringProjectPanel").hidden = true;
  $("quickAgentEditor").hidden = false;
  document.querySelector(".wizard-layout").hidden = true;
  prepareQuickCreate(detail);
  $("quickAgentPrompt").focus();
}

function quickSelectedModelIds() {
  return [...document.querySelectorAll("[data-quick-model-id]:checked")]
    .map(node => node.dataset.quickModelId);
}

function syncQuickModelSelect(preferred = $("quickAgentModel").value, fallbackName = "") {
  const selectedIds = quickSelectedModelIds();
  const selectedModels = selectedIds.map(resourceById).filter(Boolean);
  if (!selectedModels.length) {
    $("quickAgentModel").innerHTML = `<option value="">${escapeHtml(fallbackName || "当前运行环境默认模型")}</option>`;
    return;
  }
  $("quickAgentModel").innerHTML = selectedModels.map(item => `
    <option value="${escapeHtml(item.resourceId)}">${escapeHtml(item.displayName)} · ${escapeHtml(item.contract?.model || item.name || "")}</option>
  `).join("");
  $("quickAgentModel").value = selectedIds.includes(preferred) ? preferred : selectedIds[0];
}

function yamlScalar(value) {
  return JSON.stringify(String(value || ""));
}

function renderQuickManifestPreview() {
  const model = resourceById($("quickAgentModel").value);
  const reference = state.current?.draft || state.agents[0] || null;
  const modelName = model?.contract?.model || model?.name || reference?.metadata?.labels?.["agentkit.ksyun.com/model"] || "glm-5.1";
  const prompt = $("quickAgentPrompt").value || "";
  const runtimeType = $("quickAgentRuntime").value || "codex";
  const models = quickSelectedModelIds()
    .map(resourceById)
    .filter(Boolean)
    .map(item => item.contract?.model || item.name)
    .filter(Boolean);
  const agentId = $("quickAgentId").value || "review-helper";
  const runtime = runtimeRef(agentId, runtimeType);
  $("quickManifestPreview").textContent = runtimeType === "codex" ? [
      `name: ${agentId}`,
      "version: 1.0.0",
      "framework: codex",
      "artifact_type: ManagedRuntime",
      "runtime:",
      "  name: codex",
      "  version: 0.144.4",
      `model: ${modelName}`,
      ...(models.length > 1 ? ["models:", ...models.map(name => `  - ${name}`)] : []),
      "prompt: |-",
      ...prompt.split("\n").map(line => `  ${line}`)
    ].join("\n") : [
      "apiVersion: agentkit.ksyun.com/v1alpha1",
      "kind: Agent",
      "metadata:",
      `  id: ${agentId}`,
      "spec:",
      "  runtime:",
      `    type: ${runtime.type}`,
      `    projectPath: ${runtime.projectPath}`,
      `    entryPoint: ${runtime.entryPoint}`,
      `    agentVariable: ${runtime.agentVariable}`,
      "  instructions:",
      "    system: |-",
      ...prompt.split("\n").map(line => `      ${line}`)
    ].join("\n");
}

async function submitQuickCreateAgent(event) {
  event.preventDefault();
  const form = $("quickAgentEditorForm");
  if (!form.checkValidity()) {
    form.reportValidity();
    return;
  }
  const button = $("quickCreateSubmit");
  $("quickCreateError").hidden = true;
  setButtonLoading(button, true, "正在保存");
  try {
    const editingAgentId = state.editingAgentId;
    const modelResourceId = $("quickAgentModel").value || null;
    const modelResourceIds = quickSelectedModelIds();
    const spec = editingAgentId && state.current?.draft?.spec
      ? clone(state.current.draft.spec)
      : { description: "AgentKit Studio Agent" };
    spec.runtime = runtimeRef($("quickAgentId").value.trim(), $("quickAgentRuntime").value);
    spec.instructions = {
      system: $("quickAgentPrompt").value.trim(),
      task: spec.instructions?.task || ""
    };
    spec.bindings = {
      ...(spec.bindings || {}),
      modelProfileId: modelResourceId,
      modelProfileIds: modelResourceIds
    };
    const saved = editingAgentId
      ? await api(`/agents/${encodeURIComponent(editingAgentId)}`, {
        method: "PUT",
        headers: { "If-Match": String(state.current?.draft?.metadata?.revision || 1) },
        body: spec
      })
      : await api("/authoring/quick", {
        method: "POST",
        body: {
          name: $("quickAgentName").value.trim(),
          slug: $("quickAgentId").value.trim(),
          runtimeType: $("quickAgentRuntime").value,
          description: spec.description || "AgentKit Studio Agent",
          template: "blank",
          spec
        }
      });
    await refreshAgents();
    state.current = await api(`/agents/${encodeURIComponent(saved.metadata.id)}`);
    state.build = currentSuccessfulBuild(state.current);
    renderGlobalContext();
    state.editingAgentId = null;
    showToast(
      editingAgentId ? "Agent 已更新" : "Agent YAML 已保存",
      editingAgentId ? "agentengine.yaml 已回写，旧构建不会继续用于新会话。" : "现在可以构建并进入真实 Codex 会话。"
    );
    if ($("quickBuildAfterCreate").checked) {
      const build = await buildCurrentAgent({ navigate: false });
      if (build) {
        await openChat(saved.metadata.id);
        const suggested = $("quickAgentTask").value.trim();
        if (suggested) {
          $("chatInput").value = suggested;
          autoSizeComposer();
        }
      }
    } else {
      await openAgentDetail(saved.metadata.id);
    }
  } catch (error) {
    $("quickCreateError").hidden = false;
    $("quickCreateErrorMessage").textContent = error.message;
    showToast("保存失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function validateStepOne() {
  const fields = [
    $("newAgentName"),
    $("newAgentId"),
    $("agentPrompt")
  ];
  if (state.wizard.template === "research") fields.push($("researchAudience"));
  for (const field of fields) {
    if (!field.checkValidity()) {
      field.reportValidity();
      field.focus();
      return false;
    }
  }
  return true;
}

async function setWizardStep(step) {
  state.wizard.step = Math.max(1, Math.min(4, Number(step)));
  document.querySelectorAll(".wizard-panel").forEach(panel => {
    panel.classList.toggle("active", Number(panel.dataset.stepPanel) === state.wizard.step);
  });
  document.querySelectorAll(".wizard-step").forEach(button => {
    const value = Number(button.dataset.step);
    button.classList.toggle("active", value === state.wizard.step);
    button.classList.toggle("completed", value < state.wizard.step);
  });
  $("wizardPrevious").disabled = state.wizard.step === 1;
  $("wizardNext").hidden = state.wizard.step === 4;
  $("wizardCreate").hidden = state.wizard.step !== 4;
  $("wizardProgress").textContent = `第 ${state.wizard.step} 步，共 4 步`;
  if (state.wizard.step === 3 && !state.wizard.composition) {
    await composeAgent({ preservePrompt: false });
  }
  if (state.wizard.step === 4) renderReview();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function nextWizardStep() {
  if (state.wizard.step === 1) {
    if (!validateStepOne()) return;
    await composeAgent({ preservePrompt: false });
  }
  if (state.wizard.step === 2) {
    await composeAgent();
  }
  if (state.wizard.step === 3) {
    if (!$("generatedSystemPrompt").value.trim()) {
      showToast("Prompt 不完整", "请补充角色与系统提示词。", "error");
      return;
    }
  }
  await setWizardStep(state.wizard.step + 1);
}

function renderWizardCapabilities() {
  const composition = state.wizard.composition;
  if (!composition) return;
  const bindingsSupported = state.wizard.runtime !== "codex";
  populateModelSelect();
  const boundToolIds = new Set(state.wizard.selectedToolIds);
  const boundSkillIds = new Set(state.wizard.selectedSkillIds);
  const boundMcpIds = new Set(state.wizard.selectedMcpIds);
  $("agentToolList").innerHTML = state.catalog.tool
    .filter(item => item.status === "ready")
    .map(item => `
      <label class="selection-item ${boundToolIds.has(item.resourceId) ? "selected" : ""}">
        <input type="checkbox" data-tool-id="${escapeHtml(item.resourceId)}" ${boundToolIds.has(item.resourceId) ? "checked" : ""} ${bindingsSupported ? "" : "disabled"}>
        <span class="capability-icon"><svg data-icon="wrench"></svg></span>
        <span class="selection-item-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.description || "结构化 Tool Contract")} · ${escapeHtml(item.contract?.sideEffect || "none")}</span></span>
        <span class="resource-source">${escapeHtml(item.version)}</span>
      </label>
    `).join("") || '<div class="selection-item"><span class="selection-item-copy"><strong>没有可用 Tool</strong><span>可以先创建 Agent，之后再补充 Tool Contract</span></span></div>';
  const visibleSkills = state.catalog.skill.filter(item => item.status === "ready");
  $("agentSkillList").innerHTML = visibleSkills.map(item => {
    const required = state.wizard.template === "research" && item.name === "deep-research-methodology";
    return `
    <label class="selection-item ${boundSkillIds.has(item.resourceId) ? "selected" : ""}">
      <input type="checkbox" data-skill-id="${escapeHtml(item.resourceId)}" ${boundSkillIds.has(item.resourceId) ? "checked" : ""} ${required || !bindingsSupported ? "disabled" : ""}>
      <span class="capability-icon"><svg data-icon="sparkles"></svg></span>
      <span class="selection-item-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.description || "版本化 Skill")}${required ? " · 模板必需" : ""}</span></span>
      <span class="resource-source">${escapeHtml(item.version)}</span>
    </label>
  `;
  }).join("") || '<div class="selection-item"><span class="selection-item-copy"><strong>没有已安装的 Skill</strong><span>可在工程资源中导入版本化 Skill</span></span></div>';
  $("agentMcpList").innerHTML = state.catalog.mcp.length
    ? state.catalog.mcp.map(item => `
      <label class="selection-item ${boundMcpIds.has(item.resourceId) ? "selected" : ""}">
        <input type="checkbox" data-mcp-id="${escapeHtml(item.resourceId)}" ${boundMcpIds.has(item.resourceId) ? "checked" : ""} ${item.status !== "ready" || !bindingsSupported ? "disabled" : ""}>
        <span class="capability-icon"><svg data-icon="network"></svg></span>
        <span class="selection-item-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.description || "MCP Server")} · ${item.health?.toolCount || 0} Tool</span></span>
        <span class="status-badge ${item.status === "ready" ? "success" : "warning"}">${item.status === "ready" ? "Ready" : escapeHtml(item.status)}</span>
      </label>
    `).join("")
    : '<div class="selection-item"><span class="selection-item-copy"><strong>没有已连接的 MCP</strong><span>点击“连接 MCP”添加外部服务</span></span></div>';
  $("mcpMissingAlert").hidden = state.wizard.template !== "research" || composition.warnings.length === 0;
  $("mcpMissingAlert").querySelector("strong").textContent = "尚未绑定外部调研 MCP";
  $("mcpMissingAlert").querySelector("p").textContent = "可以继续创建，但 Agent 只能使用用户输入和工作区资料，并会在回答中声明限制。";
  $("skillRecommendation").hidden = state.wizard.template !== "research";
  if (!bindingsSupported) {
    $("mcpMissingAlert").hidden = false;
    $("mcpMissingAlert").querySelector("strong").textContent = "Codex 能力边界";
    $("mcpMissingAlert").querySelector("p").textContent = "CodexRuntimeAdapter 当前使用 Codex 原生工具；ksadk Tool、MCP 与 Skill 只可绑定到 ADK 或 LangGraph。";
  }
  injectIcons($("view-create"));
  renderWizardSummary();
}

function renderWizardSummary() {
  const composition = state.wizard.composition;
  const model = composition
    ? resourceById(composition.spec.bindings.modelProfileId)
    : state.catalog.model.find(item => item.resourceId === $("agentModel").value);
  const policy = policyCopy(state.wizard.policyTemplate);
  $("summaryTemplate").textContent = templateName(state.wizard.template);
  $("summaryRuntime").textContent = state.wizard.runtime === "langgraph" ? "LangGraph" : state.wizard.runtime === "adk" ? "Google ADK" : "Codex";
  $("summaryModel").textContent = model?.displayName || "待选择";
  $("summarySkills").textContent = state.wizard.selectedSkillIds.length;
  $("summaryMcp").textContent = state.wizard.selectedMcpIds.length;
  $("summaryTools").textContent = state.wizard.selectedToolIds.length;
  $("summaryStrategy").textContent = state.wizard.template === "research" ? "Plan · Act · Observe" : "Direct";
  $("summaryPolicyTitle").textContent = policy.title;
  $("summaryPolicyDescription").textContent = policy.description;
}

function renderReview() {
  const composition = state.wizard.composition;
  if (!composition) return;
  $("reviewAgentName").textContent = $("newAgentName").value.trim();
  const templateMeta = state.wizard.template === "research"
    ? depthName(state.wizard.depth)
    : templateName(state.wizard.template);
  $("reviewAgentMeta").textContent = `${$("newAgentId").value.trim()} · ${state.wizard.runtime} · ${templateMeta}`;
  $("reviewAgentGoal").textContent = $("agentDescription").value.trim() || $("agentPrompt").value.trim();
  $("reviewAgentAvatar").classList.toggle("research", state.wizard.template === "research");
  $("reviewAgentAvatar").innerHTML = `<svg data-icon="${state.wizard.template === "research" ? "search" : "bot"}"></svg>`;
  const bindings = composition.spec.bindings;
  const items = [
    ["cpu", resourceById(bindings.modelProfileId)?.displayName || "Model", "Model Profile"],
    ["wrench", `${bindings.tools.length} 个 Tool`, policyCopy(bindings.policyTemplate).title],
    ["network", `${bindings.mcpServers.length} 个 MCP`, bindings.mcpServers.length ? "已连接外部服务" : "未绑定"],
    ["sparkles", `${bindings.skills.length} 个 Skill`, bindings.skills.length ? "已注入版本化能力" : "未绑定"]
  ];
  $("reviewCapabilities").innerHTML = items.map(([icon, title, subtitle]) => `
    <div class="review-capability"><svg data-icon="${icon}"></svg><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(subtitle)}</span></div></div>
  `).join("");
  $("reviewPrompt").textContent = $("generatedSystemPrompt").value.trim();
  injectIcons($("view-create"));
}

function depthName(value) {
  return { focused: "聚焦调研", standard: "标准调研", deep: "深度调研" }[value] || value;
}

function templateName(value) {
  return value === "research" ? "Research Agent" : "空白 Agent";
}

function policyCopy(value) {
  return {
    loose: {
      title: "宽松权限策略",
      description: "已绑定 Tool 默认允许调用，适合可信的本地环境。"
    },
    strict: {
      title: "严格权限策略",
      description: "只读 Tool 自动允许，外部或写入操作需要审批。"
    },
    custom: {
      title: "自定义权限策略",
      description: "沿用每个 Tool Contract 中配置的审批策略。"
    }
  }[value] || {
    title: "严格权限策略",
    description: "只读 Tool 自动允许，外部或写入操作需要审批。"
  };
}

async function submitCreateAgent(event) {
  event.preventDefault();
  if (state.wizard.step !== 4) return;
  const button = $("wizardCreate");
  setButtonLoading(button, true, "正在创建");
  $("createError").hidden = true;
  try {
    if (!state.wizard.composition) await composeAgent({ preservePrompt: false });
    const spec = clone(state.wizard.composition.spec);
    spec.instructions = {
      system: $("generatedSystemPrompt").value.trim(),
      task: $("generatedTaskPrompt").value.trim()
    };
    spec.runtime = runtimeRef($("newAgentId").value.trim(), state.wizard.runtime);
    spec.description = $("agentDescription").value.trim() || spec.description;
    const created = await api("/authoring/quick", {
      method: "POST",
      body: {
        name: $("newAgentName").value.trim(),
        slug: $("newAgentId").value.trim(),
        runtimeType: state.wizard.runtime,
        description: spec.description,
        template: state.wizard.template,
        spec
      }
    });
    await refreshAgents();
    state.current = {
      draft: created,
      builds: [],
      validation: { valid: true, diagnostics: [] }
    };
    state.build = null;
    renderGlobalContext();
    showToast(
      "Agent 已创建",
      "YAML Revision、RuntimeRef 和能力绑定已写入工作区。"
    );
    if ($("buildAfterCreate").checked) {
      switchView("builds");
      renderBuildWorkspace();
      const build = await buildCurrentAgent({ navigate: false });
      if (build) await openChat(created.metadata.id);
    } else {
      await openAgentDetail(created.metadata.id);
    }
  } catch (error) {
    $("createError").hidden = false;
    $("createErrorMessage").textContent = error.message;
    showToast("创建失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}
