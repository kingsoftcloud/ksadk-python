async function refreshCatalog() {
  const [payload, discoveredModels] = await Promise.all([
    api("/catalog/resources?limit=200"),
    api("/catalog/models").catch(() => null)
  ]);
  state.catalog = { model: [], tool: [], mcp: [], skill: [] };
  payload.items.forEach(item => state.catalog[item.kind].push(item));
  if (discoveredModels?.items?.length) {
    state.catalog.model = [
      ...state.catalog.model.filter(item => item.source === "local" || item.source === "market"),
      ...discoveredModels.items
    ];
  }
  await refreshCredentialStatuses();
  $("modelCount").textContent = state.catalog.model.filter(item => item.status === "ready").length;
  $("capabilityCount").textContent = [
    ...state.catalog.tool,
    ...state.catalog.mcp,
    ...state.catalog.skill
  ].filter(item => item.status === "ready").length;
  populateModelSelect();
  if (state.view === "resources") renderResources();
}

async function refreshAgents() {
  $("agentSyncState").textContent = "同步中";
  const payload = await api(`/agents?limit=100&query=${encodeURIComponent($("agentSearch").value.trim())}`);
  state.agents = payload.items;
  const details = await Promise.all(
    state.agents.map(agent => api(`/agents/${encodeURIComponent(agent.metadata.id)}`))
  );
  state.agentDetails = new Map(
    details.map(detail => [detail.draft.metadata.id, detail])
  );
  selectDefaultAgent(details);
  $("agentCount").textContent = state.agents.length;
  $("agentSyncState").textContent = "已同步";
  renderAgentRows();
  renderGlobalContext();
  renderTraceAgentFilter();
}

function renderGlobalContext() {
  const select = $("globalAgentSelect");
  const currentId = state.current?.draft?.metadata?.id || "";
  select.innerHTML = state.agents.length
    ? state.agents.map(agent => `<option value="${escapeHtml(agent.metadata.id)}">${escapeHtml(agent.metadata.name)}</option>`).join("")
    : '<option value="">未选择 Agent</option>';
  if (currentId) select.value = currentId;
  const runtimeType = state.current?.draft?.spec?.runtime?.type
    || state.current?.draft?.metadata?.labels?.["agentkit.ksyun.com/framework"]
    || "";
  $("globalRuntimeBadge").textContent = runtimeType
    ? `${runtimeType} RuntimeAdapter`
    : "Runtime 未选择";
}

async function switchGlobalAgent(agentId) {
  if (!agentId || agentId === state.current?.draft?.metadata?.id) return;
  const detail = state.agentDetails.get(agentId)
    || await api(`/agents/${encodeURIComponent(agentId)}`);
  state.current = detail;
  state.build = currentSuccessfulBuild(detail);
  state.chatSessionId = null;
  state.activeRun = null;
  state.activeChatModel = "";
  renderGlobalContext();
  if (state.view === "chat") {
    await openChat(agentId);
    return;
  }
  if (state.view === "agent-detail") renderAgentDetail();
  if (state.view === "builds") renderBuildWorkspace();
  if (state.view === "observability") {
    $("traceAgentFilter").value = agentId;
    await refreshTraces();
  }
  syncBrowserRoute();
}

function renderTraceAgentFilter() {
  const select = $("traceAgentFilter");
  if (!select) return;
  const selected = select.value;
  select.innerHTML = '<option value="">全部 Agent</option>' + state.agents.map(agent => (
    `<option value="${escapeHtml(agent.metadata.id)}">${escapeHtml(agent.metadata.name)}</option>`
  )).join("");
  if (state.agents.some(agent => agent.metadata.id === selected)) select.value = selected;
}

function selectDefaultAgent(details) {
  if (!details.length) return;
  const currentId = state.current?.draft?.metadata?.id;
  const selected = details.find(detail => detail.draft.metadata.id === currentId)
    || (state.current ? null : details[0]);
  if (!selected) return;
  state.current = selected;
  state.build = currentSuccessfulBuild(selected);
  renderGlobalContext();
}

function currentSuccessfulBuild(detail = state.current) {
  if (!detail) return null;
  const successful = detail.builds?.filter(item => item.status === "SUCCEEDED") || [];
  if (detail.manifestSha256) {
    return successful.find(item => item.manifestSha256 === detail.manifestSha256) || null;
  }
  return successful[0] || null;
}

function renderAgentRows() {
  const query = $("agentSearch").value.trim().toLowerCase();
  const statusFilter = $("agentStatusFilter").value;
  const filtered = state.agents.filter(agent => {
    const detail = state.agentDetails.get(agent.metadata.id);
    const built = Boolean(detail?.builds?.some(item => item.status === "SUCCEEDED"));
    const matchesQuery = !query
      || agent.metadata.name.toLowerCase().includes(query)
      || agent.metadata.id.toLowerCase().includes(query);
    const matchesStatus = !statusFilter
      || (statusFilter === "built" && built)
      || (statusFilter === "draft" && !built);
    return matchesQuery && matchesStatus;
  });
  $("agentEmpty").hidden = filtered.length > 0;
  $("agentRows").innerHTML = filtered.map(agent => {
    const detail = state.agentDetails.get(agent.metadata.id);
    const latestBuild = currentSuccessfulBuild(detail);
    const bindings = agent.spec.bindings || {};
    const template = agent.metadata.labels?.["agentkit.ksyun.com/template"] || "blank";
    const runtimeType = agent.spec.runtime?.type
      || agent.metadata.labels?.["agentkit.ksyun.com/framework"]
      || "adk";
    return `
      <tr>
        <td>
          <div class="agent-cell">
            <span class="agent-avatar ${template === "research" ? "research" : ""}"><svg data-icon="${template === "research" ? "search" : "bot"}"></svg></span>
            <div class="agent-cell-copy">
              <strong>${escapeHtml(agent.metadata.name)}</strong>
              <span>${escapeHtml(agent.metadata.id)}</span>
            </div>
          </div>
        </td>
        <td><span class="status-badge neutral">${escapeHtml(runtimeType)}</span> <span class="meta-inline">${template === "research" ? "Research" : "Blank"}</span></td>
        <td><div class="resource-counts"><span>${bindings.tools?.length || 0} Tool</span><span>${bindings.mcpServers?.length || 0} MCP</span><span>${bindings.skills?.length || 0} Skill</span></div></td>
        <td><span class="mono">r${agent.metadata.revision}</span></td>
        <td>${latestBuild ? `<span class="status-badge success">已构建</span>` : `<span class="status-badge neutral">草稿</span>`}</td>
        <td class="actions-column">
          <button class="button tertiary small" data-open-agent="${escapeHtml(agent.metadata.id)}" type="button">配置</button>
          <button class="button tertiary small" data-edit-agent="${escapeHtml(agent.metadata.id)}" type="button">编辑</button>
          <button class="button secondary small" data-chat-agent="${escapeHtml(agent.metadata.id)}" type="button">会话</button>
          <button class="button danger small" data-delete-agent="${escapeHtml(agent.metadata.id)}" type="button">删除</button>
        </td>
      </tr>
    `;
  }).join("");
  injectIcons($("agentRows"));
}

function populateModelSelect() {
  const currentValue = $("agentModel").value;
  const options = state.catalog.model
    .filter(item => ["ready", "missing-secret"].includes(item.status))
    .map(item => {
      const status = state.credentialStatuses[modelCredentialReference(item)];
      const credentialLabel = status?.configured ? "凭证已配置" : "需配置凭证";
      return `<option value="${escapeHtml(item.resourceId)}">${escapeHtml(item.displayName)} · ${escapeHtml(item.version)} · ${credentialLabel}</option>`;
    })
    .join("");
  $("agentModel").innerHTML = options || '<option value="">没有可用模型</option>';
  const selected = state.wizard.composition?.spec?.bindings?.modelProfileId
    || currentValue;
  if (selected) $("agentModel").value = selected;
  renderSelectedModelCredentialStatus();
}

function renderSelectedModelCredentialStatus() {
  const selectedModel = resourceById($("agentModel").value);
  const credentialStatus = state.credentialStatuses[modelCredentialReference(selectedModel)];
  $("agentModelCredentialStatus").textContent = credentialStatus?.configured
    ? `凭证已配置 · ${credentialStatus.source === "session" ? "当前 Studio 会话" : "启动环境变量"}`
    : "模型凭证未配置；Agent 可以先构建，但运行前需要配置 API Key。";
}

function resourceById(resourceId) {
  return Object.values(state.catalog)
    .flat()
    .find(item => item.resourceId === resourceId);
}

function modelCredentialReference(resource) {
  return resource?.requiredSecretRefs?.[0]
    || resource?.contract?.credentialRef
    || "";
}

function credentialNameFromReference(reference) {
  return reference.startsWith("env://")
    ? reference.slice("env://".length)
    : "";
}

async function refreshCredentialStatuses() {
  const references = [...new Set(
    state.catalog.model
      .map(modelCredentialReference)
      .filter(reference => reference.startsWith("env://"))
  )];
  const statuses = await Promise.all(references.map(async reference => {
    const name = credentialNameFromReference(reference);
    try {
      return [reference, await api(`/credentials/${encodeURIComponent(name)}`)];
    } catch {
      return [reference, {
        reference,
        name,
        configured: false,
        source: "missing",
        persistence: "missing"
      }];
    }
  }));
  state.credentialStatuses = Object.fromEntries(statuses);
}
