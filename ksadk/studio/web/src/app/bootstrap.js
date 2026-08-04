function updatePromptCounter() {
  const length = $("agentPrompt").value.length;
  $("promptCounter").textContent = `${length} / 32768`;
}

function bindEvents() {
  $("createAgentButton").onclick = openCreate;
  $("globalAgentSelect").onchange = event => {
    switchGlobalAgent(event.target.value).catch(handleGlobalError);
  };
  $("executionTargetSelect").onchange = event => {
    if (event.target.value !== "local") {
      event.target.value = "local";
      showToast(
        "金山云尚未连接",
        "绑定云凭证后可在此切换云 Agent；本地版本不会返回 Mock 云状态。",
        "error"
      );
    }
  };
  $("agentRuntime").onchange = updateRuntimeUi;
  $("quickAgentRuntime").onchange = () => {
    $("quickRuntimeTitle").textContent = `${$("quickAgentRuntime").value === "adk" ? "ADK" : $("quickAgentRuntime").value === "langgraph" ? "LangGraph" : "Codex"}RuntimeAdapter`;
    renderQuickManifestPreview();
  };
  $("emptyCreateAgent").onclick = openCreate;
  $("exitCreate").onclick = () => {
    const editingAgentId = state.editingAgentId;
    state.editingAgentId = null;
    if (editingAgentId) openAgentDetail(editingAgentId).catch(handleGlobalError);
    else switchView("agents");
  };
  $("backToAgents").onclick = () => switchView("agents");
  $("globalRefresh").onclick = () => {
    if (!state.bootstrap) {
      location.reload();
      return;
    }
    refreshAll().catch(handleGlobalError);
  };
  $("workspaceSwitcher").onclick = openWorkspaceConnection;
  $("runtimeIndicator").onclick = openWorkspaceConnection;
  $("reconnectWorkspace").onclick = () => reconnectWorkspace();
  window.addEventListener("hashchange", () => reconnectSessionFromHash());
  $("mobileMenu").onclick = () => $("sidebar").classList.toggle("open");
  $("agentSearch").oninput = renderAgentRows;
  $("agentStatusFilter").onchange = renderAgentRows;
  $("agentPrompt").oninput = updatePromptCounter;
  $("wizardPrevious").onclick = () => setWizardStep(state.wizard.step - 1);
  $("wizardNext").onclick = () => nextWizardStep().catch(handleGlobalError);
  $("createAgentForm").onsubmit = event => submitCreateAgent(event);
  $("quickAgentEditorForm").onsubmit = event => submitQuickCreateAgent(event);
  $("authoringConversationSend").onclick = () => composeConversationAgent().catch(handleGlobalError);
  $("authoringConversationInput").onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composeConversationAgent().catch(handleGlobalError);
    }
  };
  $("authoringProposalForm").onsubmit = event => confirmConversationAgent(event).catch(handleGlobalError);
  $("agentImportInspectForm").onsubmit = event => inspectAgentImport(event).catch(handleGlobalError);
  $("agentImportCommitForm").onsubmit = event => commitAgentImport(event).catch(handleGlobalError);
  $("projectInspectForm").onsubmit = event => inspectAgentProject(event).catch(handleGlobalError);
  $("projectCommitForm").onsubmit = event => commitAgentProject(event).catch(handleGlobalError);
  ["quickAgentId", "quickAgentPrompt", "quickAgentModel"].forEach(id => {
    $(id).addEventListener("input", renderQuickManifestPreview);
    $(id).addEventListener("change", renderQuickManifestPreview);
  });
  $("quickAgentModels").onchange = () => {
    syncQuickModelSelect();
    renderQuickManifestPreview();
  };
  $("regeneratePrompt").onclick = () => composeAgent({ preservePrompt: false }).catch(handleGlobalError);
  $("agentModel").onchange = async () => {
    renderSelectedModelCredentialStatus();
    if (!state.wizard.composition) return;
    await composeAgent();
  };
  $("configureSelectedModel").onclick = () => {
    const resourceId = $("agentModel").value;
    if (resourceId) openModelCredential(resourceId).catch(handleGlobalError);
  };
  $("researchDepth").onclick = event => {
    const card = event.target.closest(".choice-card");
    if (!card) return;
    state.wizard.depth = card.dataset.value;
    document.querySelectorAll("#researchDepth .choice-card").forEach(node => {
      node.classList.toggle("selected", node === card);
    });
    if (state.wizard.composition) composeAgent().catch(handleGlobalError);
  };
  $("connectMcpButton").onclick = () => openOverlay("mcpOverlay");
  $("mcpTransport").onchange = updateMcpTransport;
  $("saveAndProbeMcp").onclick = () => saveAndProbeMcp();
  $("saveModelCredential").onclick = () => saveModelCredential();
  $("saveAndTestModelCredential").onclick = () => saveModelCredential({ testConnection: true });
  $("removeModelCredential").onclick = () => removeModelCredential();
  $("detailEdit").onclick = () => {
    const agentId = state.current?.draft?.metadata?.id;
    if (agentId) openEditAgent(agentId).catch(handleGlobalError);
  };
  $("detailDelete").onclick = () => {
    const agentId = state.current?.draft?.metadata?.id;
    if (agentId) openDeleteAgent(agentId);
  };
  $("detailBuild").onclick = () => buildCurrentAgent();
  $("detailChat").onclick = () => openChat().catch(handleGlobalError);
  $("detailInvoke").onclick = openInvocation;
  $("buildCurrentAgent").onclick = () => buildCurrentAgent();
  $("conversationInvoke").onclick = openInvocation;
  $("toggleInspector").onclick = () => $("runInspector").classList.toggle("open");
  $("newSession").onclick = newChatSession;
  $("confirmDeleteAgent").onclick = () => deleteAgent();
  $("confirmDeleteSession").onclick = () => deleteSession();
  $("sendMessage").onclick = () => sendChatMessage();
  $("chatModel").onchange = () => {
    state.activeChatModel = $("chatModel").value;
    renderInvocation();
  };
  $("chatInput").oninput = autoSizeComposer;
  $("chatInput").onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendChatMessage();
    }
  };
  $("copyInvocation").onclick = () => copyInvocation();
  $("addResourceButton").onclick = addResource;
  $("discoverSkills").onclick = () => discoverWorkspaceSkills().catch(handleGlobalError);
  $("commitDiscoveredSkill").onclick = () => commitSelectedSkill().catch(handleGlobalError);
  $("pythonToolForm").onsubmit = event => savePythonTool(event).catch(handleGlobalError);
  $("resourceSearch").oninput = renderResources;
  $("resourceStatusFilter").onchange = renderResources;
  $("refreshRuns").onclick = () => refreshTraces().catch(handleGlobalError);
  $("traceSearch").oninput = renderTraceList;
  $("traceAgentFilter").onchange = () => refreshTraces().catch(handleGlobalError);
  $("traceStatusFilter").onchange = () => refreshTraces().catch(handleGlobalError);
  $("openFullTrace").onclick = () => {
    const traceId = $("openFullTrace").dataset.traceId;
    if (traceId) openTrace(traceId).catch(handleGlobalError);
  };
  $("copyTraceparent").onclick = async () => {
    const trace = state.activeTrace;
    if (!trace?.traceId || !trace.rootSpanId) return;
    await navigator.clipboard.writeText(`00-${trace.traceId}-${trace.rootSpanId}-01`);
    showToast("traceparent 已复制", shortId(trace.traceId, 24));
  };
  $("toggleTraceDetail").onclick = () => setTraceDetailExpanded(!state.traceDetailExpanded);
  $("copyRawOtlp").onclick = () => copyRawTrace().catch(handleGlobalError);

  document.addEventListener("click", event => {
    const authoringMode = event.target.closest("[data-authoring-mode]");
    if (authoringMode) {
      setAuthoringMode(authoringMode.dataset.authoringMode);
      return;
    }
    const navigation = event.target.closest(".nav-item");
    if (navigation) {
      const view = navigation.dataset.view;
      if (view === "chat") openChat().catch(handleGlobalError);
      else if (view === "resources") {
        state.resourceKind = navigation.dataset.resourceKind || "model";
        renderResources();
        switchView("resources", { title: navigation.textContent.trim() });
      } else {
        if (view === "builds") renderBuildWorkspace();
        switchView(view);
        if (view === "observability") refreshTraces().catch(handleGlobalError);
      }
      return;
    }
    const configureModel = event.target.closest("[data-configure-model]");
    if (configureModel) {
      openModelCredential(configureModel.dataset.configureModel).catch(handleGlobalError);
      return;
    }
    const retryRun = event.target.closest("[data-retry-run]");
    if (retryRun) {
      const run = state.runs.find(item => item.id === retryRun.dataset.retryRun)
        || state.activeRun;
      const content = run?.input || state.lastFailedMessage;
      if (content) {
        $("chatInput").value = content;
        autoSizeComposer();
        sendChatMessage();
      }
      return;
    }
    const openAgent = event.target.closest("[data-open-agent]");
    if (openAgent) {
      openAgentDetail(openAgent.dataset.openAgent).catch(handleGlobalError);
      return;
    }
    const editAgent = event.target.closest("[data-edit-agent]");
    if (editAgent) {
      openEditAgent(editAgent.dataset.editAgent).catch(handleGlobalError);
      return;
    }
    const chatAgent = event.target.closest("[data-chat-agent]");
    if (chatAgent) {
      openChat(chatAgent.dataset.chatAgent).catch(handleGlobalError);
      return;
    }
    const deleteAgentButton = event.target.closest("[data-delete-agent]");
    if (deleteAgentButton) {
      openDeleteAgent(deleteAgentButton.dataset.deleteAgent);
      return;
    }
    const wizardStep = event.target.closest(".wizard-step");
    if (wizardStep && Number(wizardStep.dataset.step) < state.wizard.step) {
      setWizardStep(Number(wizardStep.dataset.step));
      return;
    }
    const editStep = event.target.closest("[data-edit-step]");
    if (editStep) {
      setWizardStep(Number(editStep.dataset.editStep));
      return;
    }
    const template = event.target.closest("[data-template]");
    if (template) {
      selectAgentTemplate(template.dataset.template);
      return;
    }
    const policy = event.target.closest("[data-policy]");
    if (policy) {
      state.wizard.policyTemplate = policy.dataset.policy;
      document.querySelectorAll("#policyTemplate button").forEach(button => {
        button.classList.toggle("selected", button === policy);
      });
      $("policyDescription").textContent = policyCopy(state.wizard.policyTemplate).description;
      renderWizardSummary();
      if (state.wizard.composition) composeAgent().catch(handleGlobalError);
      return;
    }
    const toolCheckbox = event.target.closest("[data-tool-id]");
    if (toolCheckbox) {
      const id = toolCheckbox.dataset.toolId;
      const selected = new Set(state.wizard.selectedToolIds);
      if (toolCheckbox.checked) selected.add(id);
      else selected.delete(id);
      state.wizard.selectedToolIds = [...selected];
      state.wizard.autoBindTools = false;
      composeAgent().catch(handleGlobalError);
      return;
    }
    const skillCheckbox = event.target.closest("[data-skill-id]");
    if (skillCheckbox) {
      const id = skillCheckbox.dataset.skillId;
      const selected = new Set(state.wizard.selectedSkillIds);
      if (skillCheckbox.checked) selected.add(id);
      else selected.delete(id);
      state.wizard.selectedSkillIds = [...selected];
      composeAgent().catch(handleGlobalError);
      return;
    }
    const mcpCheckbox = event.target.closest("[data-mcp-id]");
    if (mcpCheckbox) {
      const id = mcpCheckbox.dataset.mcpId;
      const selected = new Set(state.wizard.selectedMcpIds);
      if (mcpCheckbox.checked) selected.add(id);
      else selected.delete(id);
      state.wizard.selectedMcpIds = [...selected];
      state.wizard.autoBindMcp = false;
      composeAgent().catch(handleGlobalError);
      return;
    }
    const suggestion = event.target.closest("[data-suggestion]");
    if (suggestion) {
      $("chatInput").value = suggestion.dataset.suggestion;
      autoSizeComposer();
      $("chatInput").focus();
      return;
    }
    const sessionMenu = event.target.closest("[data-session-menu]");
    if (sessionMenu) {
      const sessionId = sessionMenu.dataset.sessionMenu;
      document.querySelectorAll("[data-session-menu-popover]").forEach(node => {
        node.hidden = node.dataset.sessionMenuPopover !== sessionId || !node.hidden;
      });
      return;
    }
    const deleteSessionButton = event.target.closest("[data-delete-session]");
    if (deleteSessionButton) {
      openDeleteSession(deleteSessionButton.dataset.deleteSession);
      return;
    }
    const session = event.target.closest("[data-session-id]");
    if (session) {
      state.chatViewRevision += 1;
      state.chatSessionId = session.dataset.sessionId;
      $("sendMessage").disabled = false;
      renderSessionList();
      renderMessages();
      const latest = agentRuns().filter(run => run.sessionId === state.chatSessionId).at(-1);
      if (latest) renderTrace(latest).catch(handleGlobalError);
      renderInvocation();
      syncBrowserRoute();
      return;
    }
    const codeTab = event.target.closest("[data-code-tab]");
    if (codeTab) {
      state.invocationTab = codeTab.dataset.codeTab;
      document.querySelectorAll("[data-code-tab]").forEach(node => {
        node.classList.toggle("active", node === codeTab);
      });
      renderInvocation();
      return;
    }
    const close = event.target.closest("[data-close-overlay]");
    if (close) {
      closeOverlay(close.dataset.closeOverlay);
      return;
    }
    const probe = event.target.closest("[data-probe-mcp]");
    if (probe) {
      probeMcp(probe.dataset.probeMcp);
      return;
    }
    const skillCandidate = event.target.closest("[data-skill-candidate]");
    if (skillCandidate) {
      $("commitDiscoveredSkill").disabled = !skillCandidate.checked;
      return;
    }
    const trace = event.target.closest("[data-open-trace]");
    if (trace) {
      openTrace(trace.dataset.openTrace).catch(handleGlobalError);
      return;
    }
    const span = event.target.closest("[data-open-span]");
    if (span) {
      state.activeSpanId = span.dataset.openSpan;
      renderTraceSpans();
      renderTraceDetail();
      return;
    }
    const traceTab = event.target.closest("[data-trace-tab]");
    if (traceTab) {
      state.traceTab = traceTab.dataset.traceTab;
      if (state.traceTab === "raw") setTraceDetailExpanded(true);
      renderTraceDetail();
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      document.querySelectorAll(".overlay:not([hidden])").forEach(node => closeOverlay(node.id));
      $("sidebar").classList.remove("open");
    }
  });
}

async function refreshAll() {
  await Promise.all([refreshCatalog(), refreshAgents(), refreshRuns()]);
  if (state.current?.draft?.metadata?.id) {
    const currentId = state.current.draft.metadata.id;
    state.current = await api(`/agents/${encodeURIComponent(currentId)}`);
    state.build = currentSuccessfulBuild(state.current);
  }
  if (state.view === "agent-detail") renderAgentDetail();
  if (state.view === "chat") {
    renderChatAgent();
    renderSessionList();
    renderMessages();
  }
  if (state.view === "observability") await refreshTraces();
}

function handleGlobalError(error) {
  console.error(error);
  showToast("操作失败", error.message || "未知错误", "error");
}

async function initialize() {
  const route = initialBrowserRoute();
  injectIcons();
  bindEvents();
  if (window.matchMedia("(max-width: 1279px)").matches) {
    $("runInspector").classList.remove("open");
  }
  updateMcpTransport();
  setRuntimeStatus("Connecting");
  try {
    await establishSession();
    await Promise.all([refreshCatalog(), refreshAgents(), refreshRuns()]);
    if (route.agentId && state.agentDetails.has(route.agentId)) {
      state.current = state.agentDetails.get(route.agentId);
      state.build = currentSuccessfulBuild(state.current);
      renderGlobalContext();
    }
    resetWizard();
    renderResources();
    if (
      route.view === "chat"
      && route.agentId
      && state.agents.some(agent => agent.metadata.id === route.agentId)
    ) {
      await openChat(route.agentId, { sessionId: route.sessionId || null });
    } else if (route.view === "observability") {
      switchView("observability");
      await refreshTraces({ selectFirst: !route.traceId });
      if (route.traceId) await openTrace(route.traceId, { switchToView: false });
    } else {
      switchView("agents");
    }
  } catch (error) {
    setRuntimeStatus("Disconnected");
    handleGlobalError(error);
  }
}

document.addEventListener("DOMContentLoaded", initialize);
