"use strict";

const iconPaths = {
  "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  "arrow-left": '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
  "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  "arrow-up": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  "bot": '<rect width="18" height="10" x="3" y="11" rx="2"/><circle cx="9" cy="16" r="1"/><circle cx="15" cy="16" r="1"/><path d="M8 7h8"/><path d="M12 3v4"/><path d="M5 11V8"/><path d="M19 11V8"/>',
  "check": '<path d="m20 6-11 11-5-5"/>',
  "chevron-down": '<path d="m6 9 6 6 6-6"/>',
  "circle-alert": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
  "cloud-upload": '<path d="M12 13v8"/><path d="m16 17-4-4-4 4"/><path d="M4.4 15.9A5 5 0 0 1 6 6.2 7 7 0 0 1 19.7 8.6 4.5 4.5 0 0 1 18.5 17H17"/>',
  "code": '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  "copy": '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  "cpu": '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M9 1v3"/><path d="M15 1v3"/><path d="M9 20v3"/><path d="M15 20v3"/><path d="M20 9h3"/><path d="M20 14h3"/><path d="M1 9h3"/><path d="M1 14h3"/>',
  "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  "menu": '<line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/>',
  "messages": '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/><path d="M8 8h8"/><path d="M8 12h5"/>',
  "network": '<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3h14v3"/><path d="M12 8v5"/>',
  "package": '<path d="m7.5 4.3 9 5.2"/><path d="M3.3 7 12 12l8.7-5"/><path d="M12 22V12"/><path d="m21 16-9 5-9-5V8l9-5 9 5Z"/>',
  "panel-right": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18"/>',
  "paperclip": '<path d="m21.4 11.6-9.7 9.7a6 6 0 0 1-8.5-8.5l10.6-10.6a4 4 0 0 1 5.7 5.7L8.8 18.6a2 2 0 0 1-2.8-2.8l9.7-9.7"/>',
  "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
  "refresh": '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
  "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  "settings": '<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.2a2 2 0 0 1-2 0l-.1-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.1.1a2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7l-.1.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.1-.1a2 2 0 0 1 2 0l.4.2a2 2 0 0 1 1 1.7v.2a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.2a2 2 0 0 1 2 0l.1.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.1-.1a2 2 0 0 1-1-1.7v-.5a2 2 0 0 1 1-1.7l.1-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.1.1a2 2 0 0 1-2 0l-.4-.2a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/>',
  "shield-check": '<path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3Z"/><path d="m9 12 2 2 4-4"/>',
  "sparkles": '<path d="m12 3-1.9 5.1L5 10l5.1 1.9L12 17l1.9-5.1L19 10l-5.1-1.9Z"/><path d="M5 3v4"/><path d="M3 5h4"/><path d="M19 17v4"/><path d="M17 19h4"/>',
  "square-pen": '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"/>',
  "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5-5l2.1 2.1-2.8 2.8-2.1-2.1a4 4 0 0 0 5 5l8.5 8.5a2 2 0 0 1-2.8 2.8Z"/>',
  "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'
};

const state = {
  csrf: "",
  sessionToken: "",
  bootstrap: null,
  agents: [],
  agentDetails: new Map(),
  catalog: { model: [], tool: [], mcp: [], skill: [] },
  credentialStatuses: {},
  current: null,
  build: null,
  runs: [],
  view: "agents",
  resourceKind: "model",
  composeSequence: 0,
  reconnecting: false,
  wizard: {
    step: 1,
    template: "blank",
    depth: "deep",
    composition: null,
    selectedToolIds: [],
    selectedSkillIds: [],
    selectedMcpIds: [],
    policyTemplate: "strict",
    autoBindTools: false,
    autoBindMcp: false,
    composing: false
  },
  chatSessionId: null,
  activeRun: null,
  activeModelResourceId: null,
  lastFailedMessage: "",
  invocationTab: "curl"
};

const terminalOperationStatuses = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "INTERRUPTED"
]);

const $ = id => document.getElementById(id);
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function injectIcons(root = document) {
  root.querySelectorAll("svg[data-icon]").forEach(svg => {
    const name = svg.dataset.icon;
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = iconPaths[name] || iconPaths["circle-alert"];
  });
}

function clone(value) {
  return structuredClone(value);
}

function operationKey(prefix) {
  return `${prefix}-${Date.now()}-${crypto.randomUUID()}`;
}

function formatDate(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function shortId(value, length = 18) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function setButtonLoading(button, loading, label = "处理中") {
  if (!button) return;
  if (loading) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span>${escapeHtml(label)}</span>`;
  } else {
    button.disabled = false;
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
      injectIcons(button);
    }
  }
}

function showToast(title, message = "", type = "success") {
  const toastKey = `${type}:${title}:${message}`;
  const duplicate = Array.from($("toastRegion").children)
    .some(node => node.dataset.toastKey === toastKey);
  if (duplicate) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.dataset.toastKey = toastKey;
  toast.innerHTML = `
    <svg data-icon="${type === "error" ? "circle-alert" : "check"}"></svg>
    <div><strong>${escapeHtml(title)}</strong>${message ? `<p>${escapeHtml(message)}</p>` : ""}</div>
  `;
  $("toastRegion").append(toast);
  injectIcons(toast);
  window.setTimeout(() => toast.remove(), type === "error" ? 7000 : 3600);
}

function setRuntimeStatus(status) {
  const ready = status === "Ready";
  const connecting = status === "Connecting";
  const dotClass = ready ? "success" : connecting ? "warning" : "danger";
  const label = ready
    ? "Local Ready"
    : connecting
    ? "Local Connecting"
    : "Local Disconnected";
  $("runtimeState").textContent = status;
  $("runtimeIndicator").querySelector(".status-dot").className = `status-dot ${dotClass}`;
  $("environmentStateDot").className = `status-dot ${dotClass}`;
  $("environmentStateLabel").textContent = label;
  $("workspaceSummary").textContent = ready
    ? state.bootstrap?.workspace?.name || "本地构建与运行"
    : connecting
    ? "正在连接本地工作区"
    : "工作区尚未连接";
  $("workspaceSwitcher").disabled = !ready;
  $("runtimeIndicator").disabled = !ready;
  $("createAgentButton").disabled = !ready;
}

async function establishSession() {
  const match = location.hash.match(/(?:^#|&)session=([^&]+)/);
  if (match) {
    state.sessionToken = decodeURIComponent(match[1]);
    const response = await fetch("/api/v1/system/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.sessionToken })
    });
    if (!response.ok) {
      state.sessionToken = "";
      throw new Error("本地 Studio 会话已失效，请重新启动服务。");
    }
    const payload = await response.json();
    state.csrf = payload.csrfToken;
    state.sessionToken = "";
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }
  state.bootstrap = await api("/system/bootstrap");
  state.csrf = state.csrf || state.bootstrap.csrfToken || "";
  $("workspaceName").textContent = state.bootstrap.workspace.name;
  $("workspacePath").textContent = state.bootstrap.workspace.path;
  setRuntimeStatus("Ready");
}

async function reconnectSessionFromHash() {
  if (!location.hash.includes("session=") || state.reconnecting) return;
  state.reconnecting = true;
  setRuntimeStatus("Connecting");
  try {
    await establishSession();
    await refreshAll();
    showToast("本地工作区已重新连接", state.bootstrap.workspace.name);
  } catch (error) {
    setRuntimeStatus("Disconnected");
    handleGlobalError(error);
  } finally {
    state.reconnecting = false;
  }
}

function openWorkspaceConnection() {
  const workspace = state.bootstrap?.workspace;
  if (!workspace) {
    showToast("工作区信息尚未就绪", "请等待 Studio 完成初始化。", "error");
    return;
  }
  $("workspaceConnectionName").textContent = workspace.name;
  $("workspaceConnectionPath").textContent = workspace.path;
  $("workspaceStatePath").textContent = `${workspace.path}/.agentkit`;
  $("workspaceConnectionStatus").className = "credential-status configured";
  $("workspaceConnectionStatus").querySelector(".status-dot").className = "status-dot success";
  $("workspaceConnectionStatusTitle").textContent = "本地工作区已连接";
  $("workspaceConnectionStatusDescription").textContent = "Agent、构建和运行记录均从当前目录加载。";
  $("workspaceConnectionError").hidden = true;
  openOverlay("workspaceOverlay");
}

async function reconnectWorkspace() {
  const workspace = state.bootstrap?.workspace;
  if (!workspace) return;
  const button = $("reconnectWorkspace");
  $("workspaceConnectionError").hidden = true;
  setButtonLoading(button, true, "正在连接");
  try {
    const connected = await api("/workspaces:open", {
      method: "POST",
      body: { path: workspace.path }
    });
    state.bootstrap.workspace = connected;
    await refreshAll();
    $("workspaceConnectionName").textContent = connected.name;
    $("workspaceConnectionPath").textContent = connected.path;
    $("workspaceStatePath").textContent = `${connected.path}/.agentkit`;
    $("workspaceConnectionStatusTitle").textContent = "本地工作区已连接";
    $("workspaceConnectionStatusDescription").textContent = "工作区数据已重新加载并完成同步。";
    setRuntimeStatus("Ready");
    showToast("本地工作区连接成功", connected.name);
  } catch (error) {
    $("workspaceConnectionStatus").className = "credential-status missing";
    $("workspaceConnectionStatus").querySelector(".status-dot").className = "status-dot warning";
    $("workspaceConnectionStatusTitle").textContent = "本地工作区连接失败";
    $("workspaceConnectionStatusDescription").textContent = "控制面无法重新加载当前工作区。";
    $("workspaceConnectionError").hidden = false;
    $("workspaceConnectionErrorMessage").textContent = error.message;
    setRuntimeStatus("Disconnected");
  } finally {
    setButtonLoading(button, false);
  }
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  let body = options.body;
  if (body !== undefined && !(body instanceof FormData) && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  if (!["GET", "HEAD"].includes(method) && state.csrf) {
    headers.set("X-CSRF-Token", state.csrf);
  }
  if (state.sessionToken) {
    headers.set("X-AgentKit-Session", state.sessionToken);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    method,
    headers,
    body
  });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const error = payload?.error || {};
    const failure = new Error(error.message || `请求失败 (${response.status})`);
    failure.code = error.code;
    failure.field = error.field;
    failure.details = error.details;
    throw failure;
  }
  return payload;
}

async function fetchSse(path) {
  const headers = {};
  if (state.sessionToken) headers["X-AgentKit-Session"] = state.sessionToken;
  const response = await fetch(`/api/v1${path}`, { headers });
  if (!response.ok) return [];
  const text = await response.text();
  return text
    .split("\n\n")
    .filter(Boolean)
    .map(block => {
      const dataLine = block.split("\n").find(line => line.startsWith("data:"));
      if (!dataLine) return null;
      try {
        return JSON.parse(dataLine.slice(5).trim());
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function setBreadcrumb(title, parent = null) {
  $("breadcrumb").innerHTML = parent
    ? `<span class="muted">${escapeHtml(parent)}</span><svg data-icon="chevron-down"></svg><span>${escapeHtml(title)}</span>`
    : `<span>${escapeHtml(title)}</span>`;
  injectIcons($("breadcrumb"));
}

function switchView(view, { parent = null, title = null } = {}) {
  state.view = view;
  document.querySelectorAll(".view").forEach(node => {
    node.classList.toggle("active", node.id === `view-${view}`);
  });
  document.querySelectorAll(".nav-item").forEach(node => {
    const matchesView = node.dataset.view === view;
    const matchesResource = view !== "resources"
      || node.dataset.resourceKind === state.resourceKind;
    const active = (matchesView && matchesResource)
      || (view === "agent-detail" && node.dataset.view === "agents")
      || (view === "create" && node.dataset.view === "agents");
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  const viewNode = $(`view-${view}`);
  setBreadcrumb(title || viewNode?.dataset.title || view, parent);
  $("sidebar").classList.remove("open");
}

async function refreshCatalog() {
  const payload = await api("/catalog/resources?limit=200");
  state.catalog = { model: [], tool: [], mcp: [], skill: [] };
  payload.items.forEach(item => state.catalog[item.kind].push(item));
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
  $("agentCount").textContent = state.agents.length;
  $("agentSyncState").textContent = "已同步";
  renderAgentRows();
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
    const latestBuild = detail?.builds?.find(item => item.status === "SUCCEEDED");
    const bindings = agent.spec.bindings || {};
    const template = agent.metadata.labels?.["agentkit.ksyun.com/template"] || "blank";
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
        <td><span class="status-badge neutral">${template === "research" ? "Research" : "Blank"}</span></td>
        <td><div class="resource-counts"><span>${bindings.tools?.length || 0} Tool</span><span>${bindings.mcpServers?.length || 0} MCP</span><span>${bindings.skills?.length || 0} Skill</span></div></td>
        <td><span class="mono">r${agent.metadata.revision}</span></td>
        <td>${latestBuild ? `<span class="status-badge success">已构建</span>` : `<span class="status-badge neutral">草稿</span>`}</td>
        <td class="actions-column">
          <button class="button tertiary small" data-open-agent="${escapeHtml(agent.metadata.id)}" type="button">配置</button>
          <button class="button secondary small" data-chat-agent="${escapeHtml(agent.metadata.id)}" type="button">会话</button>
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
  resetWizard();
  switchView("create", { parent: "Agent", title: "创建 Agent" });
  $("newAgentName").focus();
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
  populateModelSelect();
  const boundToolIds = new Set(state.wizard.selectedToolIds);
  const boundSkillIds = new Set(state.wizard.selectedSkillIds);
  const boundMcpIds = new Set(state.wizard.selectedMcpIds);
  $("agentToolList").innerHTML = state.catalog.tool
    .filter(item => item.status === "ready")
    .map(item => `
      <label class="selection-item ${boundToolIds.has(item.resourceId) ? "selected" : ""}">
        <input type="checkbox" data-tool-id="${escapeHtml(item.resourceId)}" ${boundToolIds.has(item.resourceId) ? "checked" : ""}>
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
      <input type="checkbox" data-skill-id="${escapeHtml(item.resourceId)}" ${boundSkillIds.has(item.resourceId) ? "checked" : ""} ${required ? "disabled" : ""}>
      <span class="capability-icon"><svg data-icon="sparkles"></svg></span>
      <span class="selection-item-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.description || "版本化 Skill")}${required ? " · 模板必需" : ""}</span></span>
      <span class="resource-source">${escapeHtml(item.version)}</span>
    </label>
  `;
  }).join("") || '<div class="selection-item"><span class="selection-item-copy"><strong>没有已安装的 Skill</strong><span>可在工程资源中导入版本化 Skill</span></span></div>';
  $("agentMcpList").innerHTML = state.catalog.mcp.length
    ? state.catalog.mcp.map(item => `
      <label class="selection-item ${boundMcpIds.has(item.resourceId) ? "selected" : ""}">
        <input type="checkbox" data-mcp-id="${escapeHtml(item.resourceId)}" ${boundMcpIds.has(item.resourceId) ? "checked" : ""} ${item.status !== "ready" ? "disabled" : ""}>
        <span class="capability-icon"><svg data-icon="network"></svg></span>
        <span class="selection-item-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.description || "MCP Server")} · ${item.health?.toolCount || 0} Tool</span></span>
        <span class="status-badge ${item.status === "ready" ? "success" : "warning"}">${item.status === "ready" ? "Ready" : escapeHtml(item.status)}</span>
      </label>
    `).join("")
    : '<div class="selection-item"><span class="selection-item-copy"><strong>没有已连接的 MCP</strong><span>点击“连接 MCP”添加外部服务</span></span></div>';
  $("mcpMissingAlert").hidden = state.wizard.template !== "research" || composition.warnings.length === 0;
  $("skillRecommendation").hidden = state.wizard.template !== "research";
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
  $("reviewAgentMeta").textContent = `${$("newAgentId").value.trim()} · ${templateMeta}`;
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
    spec.description = $("agentDescription").value.trim() || spec.description;
    const created = await api("/agents", {
      method: "POST",
      body: {
        id: $("newAgentId").value.trim(),
        name: $("newAgentName").value.trim(),
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
    showToast("Agent 已创建", "系统提示词和能力绑定已写入 Agent Draft。");
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

async function openAgentDetail(agentId) {
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  state.current = detail;
  state.build = detail.builds.find(item => item.status === "SUCCEEDED") || null;
  renderAgentDetail();
  switchView("agent-detail", { parent: "Agent", title: detail.draft.metadata.name });
}

function renderAgentDetail() {
  if (!state.current) return;
  const draft = state.current.draft;
  const bindings = draft.spec.bindings || {};
  $("detailAgentName").textContent = draft.metadata.name;
  $("detailAgentMeta").textContent = `${draft.metadata.id} · revision ${draft.metadata.revision}`;
  $("detailSystemPrompt").textContent = draft.spec.instructions.system;
  $("detailTaskPrompt").textContent = draft.spec.instructions.task || "未配置任务契约";
  const groups = [
    ["Model", bindings.modelProfileId ? [bindings.modelProfileId] : []],
    ["Skill", (bindings.skills || []).map(item => item.resourceId)],
    ["MCP", (bindings.mcpServers || []).map(item => item.resourceId)],
    ["Tool", (bindings.tools || []).map(item => item.resourceId)]
  ];
  $("detailBindings").innerHTML = groups.map(([name, ids]) => `
    <div class="binding-group"><span>${name}</span><div class="binding-items">${
      ids.length
        ? ids.map(id => {
          const resource = resourceById(id);
          return `<span class="compact-resource"><svg data-icon="check"></svg>${escapeHtml(resource?.displayName || shortId(id, 28))}</span>`;
        }).join("")
        : '<span class="compact-resource">未绑定</span>'
    }</div></div>
  `).join("");
  $("detailSummary").innerHTML = `
    <div><dt>Revision</dt><dd>r${draft.metadata.revision}</dd></div>
    <div><dt>策略</dt><dd>${escapeHtml(draft.spec.execution.strategy)}</dd></div>
    <div><dt>最大步骤</dt><dd>${draft.spec.execution.maxSteps}</dd></div>
    <div><dt>超时</dt><dd>${draft.spec.execution.timeoutSeconds}s</dd></div>
  `;
  const latestBuild = state.current.builds.find(item => item.status === "SUCCEEDED");
  $("detailBuildState").innerHTML = latestBuild
    ? `<span class="status-dot success"></span><div><strong>Bundle 已就绪</strong><span>${escapeHtml(shortId(latestBuild.bundleDigest, 28))}</span></div>`
    : '<span class="status-dot neutral"></span><div><strong>尚未构建</strong><span>创建 Bundle 后即可对话</span></div>';
  injectIcons($("view-agent-detail"));
}

async function buildCurrentAgent({ navigate = true } = {}) {
  if (!state.current) {
    showToast("没有选择 Agent", "请先从 Agent 列表选择一个 Agent。", "error");
    return null;
  }
  if (navigate) switchView("builds");
  renderBuildWorkspace();
  const draft = state.current.draft;
  $("buildLog").textContent = "提交本地构建...\n";
  setStatusBadge($("buildStatus"), "QUEUED");
  const button = $("buildCurrentAgent");
  setButtonLoading(button, true, "构建中");
  try {
    const operation = await api(`/agents/${encodeURIComponent(draft.metadata.id)}/builds`, {
      method: "POST",
      headers: { "Idempotency-Key": `build-${draft.metadata.id}-r${draft.metadata.revision}-${Date.now()}` },
      body: { revision: draft.metadata.revision, runEvaluation: false }
    });
    $("buildOperationId").textContent = operation.id;
    const completed = await waitOperation(operation.id, {
      onEvents: events => appendBuildEvents(events),
      onStatus: status => setStatusBadge($("buildStatus"), status)
    });
    if (completed.status !== "SUCCEEDED") {
      throw new Error(completed.error?.message || "构建未完成");
    }
    state.build = await api(`/builds/${encodeURIComponent(completed.resourceId)}`);
    const refreshed = await api(`/agents/${encodeURIComponent(draft.metadata.id)}`);
    state.current = refreshed;
    state.agentDetails.set(draft.metadata.id, refreshed);
    renderBuildWorkspace();
    showToast("AgentBundle 构建完成", shortId(state.build.bundleDigest, 36));
    return state.build;
  } catch (error) {
    setStatusBadge($("buildStatus"), "FAILED");
    $("buildLog").textContent += `\n${error.message}\n`;
    showToast("构建失败", error.message, "error");
    return null;
  } finally {
    setButtonLoading(button, false);
  }
}

function renderBuildWorkspace() {
  const draft = state.current?.draft;
  const build = state.build || state.current?.builds?.find(item => item.status === "SUCCEEDED");
  $("buildAgentName").textContent = draft?.metadata.name || "未选择";
  $("buildRevision").textContent = draft ? `r${draft.metadata.revision}` : "-";
  $("buildDigest").textContent = build?.bundleDigest || "-";
  setStatusBadge($("buildStatus"), build?.status || "IDLE");
  if (build) {
    $("buildLog").textContent = [
      `Build       ${build.id}`,
      `Agent       ${build.agentId}`,
      `Revision    ${build.sourceRevision}`,
      `Resolved    ${build.resolvedDigest}`,
      `Bundle      ${build.bundleDigest}`,
      `Status      ${build.status}`
    ].join("\n");
  }
}

function appendBuildEvents(events) {
  if (!events.length) return;
  $("buildLog").textContent += events
    .map(event => `${String(event.id).padStart(2, "0")}  ${event.type}`)
    .join("\n") + "\n";
  $("buildLog").scrollTop = $("buildLog").scrollHeight;
}

async function waitOperation(operationId, { onEvents = () => {}, onStatus = () => {} } = {}) {
  let cursor = 0;
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const events = await fetchSse(`/operations/${encodeURIComponent(operationId)}/events?after=${cursor}`);
    if (events.length) {
      cursor = Math.max(cursor, ...events.map(event => event.id));
      onEvents(events);
    }
    const operation = await api(`/operations/${encodeURIComponent(operationId)}`);
    onStatus(operation.status, operation);
    if (terminalOperationStatuses.has(operation.status)) return operation;
    await sleep(200);
  }
  throw new Error("操作等待超时");
}

function setStatusBadge(node, status) {
  if (!node) return;
  const value = String(status || "IDLE");
  node.className = `status-badge ${value}`;
  node.textContent = value;
}

async function openChat(agentId = state.current?.draft?.metadata?.id) {
  if (!agentId) {
    const first = state.agents[0];
    if (!first) {
      openCreate();
      return;
    }
    agentId = first.metadata.id;
  }
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  state.current = detail;
  state.build = detail.builds.find(item => item.status === "SUCCEEDED") || null;
  state.chatSessionId = null;
  renderChatAgent();
  switchView("chat");
  if (state.bootstrap?.features?.sharedChat) {
    mountSharedChat(agentId);
    renderInvocation();
    return;
  }
  mountLegacyChat();
  await refreshRuns();
  renderMessages();
  renderInvocation();
}

function mountSharedChat(agentId) {
  const host = $("sharedChatHost");
  const legacy = $("legacyChatShell");
  const frame = $("sharedChatFrame");
  host.hidden = false;
  legacy.hidden = true;
  if (frame.dataset.themeListener !== "bound") {
    frame.dataset.themeListener = "bound";
    frame.addEventListener("load", () => applySharedChatTheme(frame));
  }
  const source = `/chat/?agentId=${encodeURIComponent(agentId)}`;
  if (frame.dataset.source !== source) {
    frame.dataset.source = source;
    frame.src = source;
  } else {
    applySharedChatTheme(frame);
  }
}

function applySharedChatTheme(frame) {
  try {
    const document = frame.contentDocument;
    if (!document?.head) return;
    document.documentElement.dataset.agentkitStudioChat = "workbench";
    if (document.getElementById("agentkitStudioSharedChatTheme")) return;
    const stylesheet = document.createElement("link");
    stylesheet.id = "agentkitStudioSharedChatTheme";
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/static/shared-chat.css";
    document.head.append(stylesheet);
  } catch {
    // The official shared UI remains usable if the optional Studio theme cannot load.
  }
}

function mountLegacyChat() {
  $("sharedChatHost").hidden = true;
  $("legacyChatShell").hidden = false;
}

function renderChatAgent() {
  const draft = state.current?.draft;
  const name = draft?.metadata.name || "选择一个 Agent";
  const template = draft?.metadata?.labels?.["agentkit.ksyun.com/template"] || "blank";
  const research = template === "research";
  $("chatAgentLabel").textContent = name;
  $("conversationAgentName").textContent = name;
  $("conversationAgentMeta").textContent = draft
    ? `${draft.metadata.id} · revision ${draft.metadata.revision}`
    : "等待选择 Agent";
  $("conversationAgentAvatar").classList.toggle("research", research);
  $("conversationAgentAvatar").innerHTML = `<svg data-icon="${research ? "search" : "bot"}"></svg>`;
  $("chatEmptyTitle").textContent = research ? "开始一次深度调研" : "开始与 Agent 对话";
  $("chatEmptyDescription").textContent = research
    ? "发送研究问题后，Agent 会先规划任务，再结合绑定的 Skill、MCP 和 Tool 逐步执行。"
    : "发送一条消息，Agent 会根据系统提示词和已绑定能力执行任务。";
  $("chatSuggestions").innerHTML = research
    ? `
      <button data-suggestion="先把我的调研目标拆成问题树，并给出执行计划。" type="button">拆解问题并规划</button>
      <button data-suggestion="先检查当前可用来源，并说明可能的信息缺口。" type="button">检查来源与缺口</button>
      <button data-suggestion="按证据质量设计最终报告结构。" type="button">设计报告结构</button>
    `
    : `
      <button data-suggestion="先介绍你的职责、能力和工作边界。" type="button">查看 Agent 能力</button>
      <button data-suggestion="根据当前上下文给出一个清晰的执行计划。" type="button">生成执行计划</button>
      <button data-suggestion="列出完成任务还需要我提供的信息。" type="button">检查输入缺口</button>
    `;
  $("chatInput").placeholder = research
    ? "输入调研问题，Enter 发送，Shift + Enter 换行"
    : "输入消息，Enter 发送，Shift + Enter 换行";
  const bindings = draft?.spec?.bindings || {};
  $("inspectorAgentSummary").innerHTML = draft
    ? `
      <div><dt>Revision</dt><dd>r${draft.metadata.revision}</dd></div>
      <div><dt>Model</dt><dd>${escapeHtml(resourceById(bindings.modelProfileId)?.displayName || "未选择")}</dd></div>
      <div><dt>Skill</dt><dd>${bindings.skills?.length || 0}</dd></div>
      <div><dt>MCP</dt><dd>${bindings.mcpServers?.length || 0}</dd></div>
      <div><dt>Tool</dt><dd>${bindings.tools?.length || 0}</dd></div>
    `
    : "";
  injectIcons($("view-chat"));
}

async function refreshRuns() {
  const payload = await api("/runs");
  state.runs = payload.items;
  renderSessionList();
  renderRunRows();
}

function agentRuns() {
  const agentId = state.current?.draft?.metadata?.id;
  return state.runs.filter(run => !agentId || run.agentId === agentId);
}

function groupedSessions() {
  const groups = new Map();
  agentRuns().forEach(run => {
    if (!groups.has(run.sessionId)) groups.set(run.sessionId, []);
    groups.get(run.sessionId).push(run);
  });
  return [...groups.entries()]
    .map(([sessionId, runs]) => ({ sessionId, runs }))
    .sort((a, b) => {
      const aDate = new Date(a.runs.at(-1)?.startedAt || 0).getTime();
      const bDate = new Date(b.runs.at(-1)?.startedAt || 0).getTime();
      return bDate - aDate;
    });
}

function renderSessionList() {
  const sessions = groupedSessions();
  $("sessionList").innerHTML = sessions.length
    ? sessions.map(({ sessionId, runs }) => {
      const firstInput = runs[0]?.input || "新会话";
      const latest = runs.at(-1);
      return `
        <button class="session-item ${state.chatSessionId === sessionId ? "active" : ""}" data-session-id="${escapeHtml(sessionId)}" type="button">
          <strong>${escapeHtml(shortId(firstInput, 34))}</strong>
          <span>${runs.length} 次运行 · ${escapeHtml(latest?.status || "CREATED")}</span>
          <small>${formatDate(latest?.startedAt || latest?.completedAt)}</small>
        </button>
      `;
    }).join("")
    : '<div class="session-empty">当前 Agent 还没有会话</div>';
}

function renderMessages() {
  const runs = state.chatSessionId
    ? agentRuns().filter(run => run.sessionId === state.chatSessionId)
    : [];
  $("chatEmpty").hidden = runs.length > 0;
  document.querySelectorAll("#messageList .message").forEach(node => node.remove());
  runs.forEach(run => {
    appendMessage("user", run.input, { time: run.startedAt });
    appendMessage(
      run.status === "COMPLETED" ? "assistant" : "error",
      run.output || run.error?.message || `运行状态：${run.status}`,
      {
        runId: run.id,
        time: run.completedAt,
        errorCode: run.error?.code || "",
        retryRunId: run.id
      }
    );
  });
  scrollMessages();
}

function appendMessage(
  role,
  content,
  {
    loading = false,
    runId = "",
    time = null,
    errorCode = "",
    retryRunId = ""
  } = {}
) {
  $("chatEmpty").hidden = true;
  const node = document.createElement("article");
  node.className = `message ${role}`;
  if (loading) node.dataset.loadingMessage = "true";
  const author = role === "user" ? "你" : role === "error" ? "运行错误" : state.current?.draft?.metadata?.name || "Agent";
  const modelResourceId = state.current?.draft?.spec?.bindings?.modelProfileId || "";
  const recovery = role === "error" && errorCode === "SECRET_NOT_FOUND" && modelResourceId
    ? `
      <div class="message-actions">
        <button class="button secondary small" data-configure-model="${escapeHtml(modelResourceId)}" type="button">配置模型凭证</button>
        <button class="button tertiary small" data-retry-run="${escapeHtml(retryRunId)}" type="button">重新发送</button>
      </div>
    `
    : "";
  node.innerHTML = `
    <div class="message-meta"><strong>${escapeHtml(author)}</strong><span>${time ? formatDate(time) : "刚刚"}</span>${runId ? `<span>${escapeHtml(shortId(runId, 18))}</span>` : ""}</div>
    <div class="message-content">${loading ? '<span class="message-loading"><i></i><i></i><i></i></span>' : escapeHtml(content)}${recovery}</div>
  `;
  $("messageList").append(node);
  return node;
}

function scrollMessages() {
  $("messageList").scrollTop = $("messageList").scrollHeight;
}

function autoSizeComposer() {
  const input = $("chatInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

async function ensureChatBuild() {
  if (state.build?.status === "SUCCEEDED") return state.build;
  const latest = state.current?.builds?.find(item => item.status === "SUCCEEDED");
  if (latest) {
    state.build = latest;
    return latest;
  }
  showToast("正在准备 AgentBundle", "第一次对话前需要完成本地构建。");
  return buildCurrentAgent({ navigate: false });
}

async function sendChatMessage() {
  const input = $("chatInput");
  const content = input.value.trim();
  if (!content || $("sendMessage").disabled) return;
  if (!state.current) {
    showToast("没有选择 Agent", "请先创建或选择一个 Agent。", "error");
    return;
  }
  $("sendMessage").disabled = true;
  input.value = "";
  autoSizeComposer();
  appendMessage("user", content);
  const loading = appendMessage("assistant", "", { loading: true });
  scrollMessages();
  try {
    const build = await ensureChatBuild();
    if (!build) throw new Error("AgentBundle 尚未就绪");
    const body = {
      input: { role: "user", content },
      environment: "local",
      stream: true
    };
    if (state.chatSessionId) body.sessionId = state.chatSessionId;
    const operation = await api(`/builds/${encodeURIComponent(build.id)}/runs`, {
      method: "POST",
      headers: { "Idempotency-Key": operationKey("chat") },
      body
    });
    setStatusBadge($("inspectorStatus"), "RUNNING");
    $("inspectorRunId").textContent = operation.id;
    const operationEvents = [];
    const completed = await waitOperation(operation.id, {
      onEvents: events => {
        operationEvents.push(...events);
        renderTimeline(operationEvents);
      },
      onStatus: status => setStatusBadge($("inspectorStatus"), status)
    });
    if (completed.status !== "SUCCEEDED") {
      const failure = new Error(completed.error?.message || "Agent 运行失败");
      failure.code = completed.error?.code || "";
      throw failure;
    }
    const run = await api(`/runs/${encodeURIComponent(completed.resourceId)}`);
    state.activeRun = run;
    state.chatSessionId = run.sessionId;
    loading.remove();
    const completedSuccessfully = run.status === "COMPLETED";
    appendMessage(
      completedSuccessfully ? "assistant" : "error",
      completedSuccessfully
        ? run.output
        : run.error?.message || `Agent 运行状态：${run.status}`,
      {
        runId: run.id,
        time: run.completedAt,
        errorCode: run.error?.code || "",
        retryRunId: run.id
      }
    );
    if (run.error?.code === "SECRET_NOT_FOUND") {
      state.lastFailedMessage = content;
    }
    await refreshRuns();
    renderSessionList();
    await renderTrace(run);
    renderInvocation();
    if (!completedSuccessfully) {
      showToast(
        "Agent 运行未完成",
        run.error?.message || `运行状态：${run.status}`,
        "error"
      );
    }
  } catch (error) {
    loading.remove();
    if (error.code === "SECRET_NOT_FOUND") {
      state.lastFailedMessage = content;
    }
    appendMessage("error", error.message, { errorCode: error.code || "" });
    setStatusBadge($("inspectorStatus"), "FAILED");
    showToast("运行失败", error.message, "error");
  } finally {
    $("sendMessage").disabled = false;
    input.focus();
    scrollMessages();
  }
}

async function renderTrace(run) {
  const trace = await api(`/traces/${encodeURIComponent(run.traceId)}`);
  renderTimeline(trace.events);
  $("inspectorRunId").textContent = run.id;
  setStatusBadge($("inspectorStatus"), run.status);
  $("usageInput").textContent = run.usage?.inputTokens || 0;
  $("usageOutput").textContent = run.usage?.outputTokens || 0;
  $("usageDuration").textContent = `${run.durationMs || 0} ms`;
}

function renderTimeline(events) {
  $("eventTimeline").innerHTML = events.length
    ? events.map(event => `
      <div class="timeline-event">
        <span class="timeline-marker"></span>
        <div class="timeline-copy"><strong>${escapeHtml(event.type)}</strong><span>${escapeHtml(eventSummary(event))}</span></div>
      </div>
    `).join("")
    : '<div class="timeline-empty">发送消息后显示模型和 Tool 事件</div>';
}

function eventSummary(event) {
  const data = event.data || {};
  if (data.model) return `${data.model} · step ${data.step || 1}`;
  if (data.tool) return data.tool;
  if (data.usage) return `${data.usage.totalTokens || 0} tokens`;
  if (data.output) return shortId(data.output, 60);
  return formatDate(event.createdAt);
}

function newChatSession() {
  state.chatSessionId = null;
  state.activeRun = null;
  renderSessionList();
  renderMessages();
  $("eventTimeline").innerHTML = '<div class="timeline-empty">发送消息后显示模型和 Tool 事件</div>';
  setStatusBadge($("inspectorStatus"), "IDLE");
  $("inspectorRunId").textContent = "尚未运行";
  $("chatInput").focus();
  renderInvocation();
}

function openOverlay(id) {
  $(id).hidden = false;
  document.body.style.overflow = "hidden";
  const focusable = $(id).querySelector("input, textarea, select, button");
  window.setTimeout(() => focusable?.focus(), 0);
}

function closeOverlay(id) {
  $(id).hidden = true;
  document.body.style.overflow = "";
}

function updateMcpTransport() {
  const stdio = $("mcpTransport").value === "stdio";
  $("mcpCommandField").hidden = !stdio;
  $("mcpArgsField").hidden = !stdio;
  $("mcpEndpointField").hidden = stdio;
  $("mcpCommand").required = stdio;
  $("mcpEndpoint").required = !stdio;
}

async function saveAndProbeMcp() {
  if (!$("mcpForm").reportValidity()) return;
  $("mcpError").hidden = true;
  const button = $("saveAndProbeMcp");
  setButtonLoading(button, true, "正在探测");
  try {
    const transport = $("mcpTransport").value;
    const envName = $("mcpEnvName").value.trim();
    const secretRef = $("mcpSecretRef").value.trim();
    const server = {
      name: $("mcpName").value.trim(),
      version: $("mcpVersion").value.trim(),
      transport,
      args: transport === "stdio"
        ? $("mcpArgs").value.trim().split(/\s+/).filter(Boolean)
        : [],
      envRefs: envName && secretRef ? { [envName]: secretRef } : {}
    };
    if (transport === "stdio") server.command = $("mcpCommand").value.trim();
    else server.endpointUrl = $("mcpEndpoint").value.trim();
    const created = await api("/catalog/mcp-servers", {
      method: "POST",
      body: {
        displayName: $("mcpDisplayName").value.trim(),
        description: $("mcpDescription").value.trim(),
        server
      }
    });
    const probed = await api(`/catalog/mcp-servers/${encodeURIComponent(created.resourceId)}:probe?timeoutSeconds=15`, {
      method: "POST"
    });
    await refreshCatalog();
    state.wizard.autoBindMcp = false;
    state.wizard.selectedMcpIds = [probed.resourceId];
    await composeAgent();
    closeOverlay("mcpOverlay");
    $("mcpForm").reset();
    updateMcpTransport();
    showToast("MCP 已连接", `已发现 ${probed.health?.toolCount || 0} 个 Tool。`);
  } catch (error) {
    $("mcpError").hidden = false;
    $("mcpErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

function openInvocation() {
  renderInvocation();
  openOverlay("invokeOverlay");
}

function renderInvocation() {
  const buildId = state.build?.id || state.current?.builds?.find(item => item.status === "SUCCEEDED")?.id;
  const sessionId = state.chatSessionId;
  const endpoint = buildId
    ? `/api/v1/builds/${buildId}/runs`
    : "/api/v1/builds/{buildId}/runs";
  $("invokeEndpoint").textContent = `POST ${endpoint}`;
  $("invokeBuildId").textContent = buildId || "尚未构建";
  $("invokeSessionId").textContent = sessionId || "首次调用可省略";
  const template = state.current?.draft?.metadata?.labels?.["agentkit.ksyun.com/template"] || "blank";
  const body = {
    ...(sessionId ? { sessionId } : {}),
    input: {
      role: "user",
      content: template === "research"
        ? "调研 Agent 工程平台的核心能力"
        : "请根据你的职责处理这个请求"
    },
    environment: "local",
    stream: true
  };
  const code = state.invocationTab === "curl"
    ? [
      `curl -X POST "http://127.0.0.1:7831${endpoint}" \\`,
      '  -H "Content-Type: application/json" \\',
      '  -H "X-AgentKit-Session: <STUDIO_SESSION_TOKEN>" \\',
      '  -H "X-CSRF-Token: <CSRF_TOKEN>" \\',
      `  -H "Idempotency-Key: run-$(date +%s)" \\`,
      `  -d '${JSON.stringify(body, null, 2)}'`
    ].join("\n")
    : [
      `const response = await fetch("${endpoint}", {`,
      '  method: "POST",',
      "  headers: {",
      '    "Content-Type": "application/json",',
      '    "X-CSRF-Token": csrfToken,',
      '    "Idempotency-Key": crypto.randomUUID()',
      "  },",
      `  body: JSON.stringify(${JSON.stringify(body, null, 2)})`,
      "});",
      "const operation = await response.json();"
    ].join("\n");
  $("invocationCode").textContent = code;
}

async function copyInvocation() {
  await navigator.clipboard.writeText($("invocationCode").textContent);
  const label = $("copyInvocation").querySelector("span");
  label.textContent = "已复制";
  window.setTimeout(() => { label.textContent = "复制"; }, 1400);
}

function renderResources() {
  const names = {
    model: ["模型", "管理 Model Profile、Endpoint 和凭据引用。"],
    tool: ["Tool", "管理结构化 Tool Contract、权限和审批策略。"],
    mcp: ["MCP", "连接、探测并复用 MCP Server。"],
    skill: ["Skill", "安装版本化 Skill，并在构建时锁定内容摘要。"]
  };
  $("resourcePageTitle").textContent = names[state.resourceKind][0];
  $("resourcePageDescription").textContent = names[state.resourceKind][1];
  $("addResourceButton").querySelector("span").textContent = state.resourceKind === "model"
    ? "配置模型"
    : state.resourceKind === "skill"
    ? "安装 Skill"
    : "添加资源";
  const query = $("resourceSearch").value.trim().toLowerCase();
  const status = $("resourceStatusFilter").value;
  const items = state.catalog[state.resourceKind].filter(item => {
    const matchesQuery = !query
      || item.displayName.toLowerCase().includes(query)
      || item.name.toLowerCase().includes(query)
      || item.description.toLowerCase().includes(query);
    const reference = item.kind === "model" ? modelCredentialReference(item) : "";
    const effectiveStatus = item.kind === "model"
      ? state.credentialStatuses[reference]?.configured
        ? "ready"
        : "missing-secret"
      : item.status;
    return matchesQuery && (!status || effectiveStatus === status);
  });
  $("resourceEmpty").hidden = items.length > 0;
  $("resourceRows").innerHTML = items.map(item => `
    <tr>
      <td><div class="agent-cell"><span class="capability-icon"><svg data-icon="${resourceIcon(item.kind)}"></svg></span><div class="agent-cell-copy"><strong>${escapeHtml(item.displayName)}</strong><span>${escapeHtml(item.name)}</span></div></div></td>
      <td>${escapeHtml(item.source)}</td>
      <td><span class="mono">${escapeHtml(item.version)}</span></td>
      <td>${escapeHtml(item.description || "未提供说明")}</td>
      <td>${resourceStatusMarkup(item)}</td>
      <td class="actions-column">${resourceActionMarkup(item)}</td>
    </tr>
  `).join("");
  injectIcons($("resourceRows"));
}

function resourceStatusMarkup(item) {
  if (item.kind === "model") {
    const reference = modelCredentialReference(item);
    const status = state.credentialStatuses[reference];
    const configured = Boolean(status?.configured);
    return `<span class="status-badge ${configured ? "success" : "warning"}">${configured ? "凭证已配置" : "凭证未配置"}</span>`;
  }
  return `<span class="status-badge ${item.status === "ready" ? "success" : "warning"}">${escapeHtml(item.status)}</span>`;
}

function resourceActionMarkup(item) {
  if (item.kind === "model") {
    return `<button class="button secondary small" data-configure-model="${escapeHtml(item.resourceId)}" type="button">配置凭证</button>`;
  }
  if (item.kind === "mcp" && item.source === "local") {
    return `<button class="button secondary small" data-probe-mcp="${escapeHtml(item.resourceId)}" type="button">重新探测</button>`;
  }
  return '<button class="button tertiary small" type="button">查看</button>';
}

function resourceIcon(kind) {
  return { model: "cpu", tool: "wrench", mcp: "network", skill: "sparkles" }[kind] || "database";
}

function renderModelCredentialStatus(status) {
  const configured = Boolean(status?.configured);
  const source = status?.source || "missing";
  $("modelCredentialStatus").className = `credential-status ${configured ? "configured" : "missing"}`;
  $("modelCredentialStatus").querySelector(".status-dot").className = `status-dot ${configured ? "success" : "warning"}`;
  $("modelCredentialStatusTitle").textContent = configured ? "模型凭证已配置" : "模型凭证未配置";
  $("modelCredentialStatusDescription").textContent = source === "session"
    ? "凭证保存在当前 Studio 会话内存中，Runtime 已可直接使用。"
    : source === "environment"
    ? "凭证由 Studio 启动环境变量提供，可以用新的会话凭证临时覆盖。"
    : "输入 API Key 后即可在本地运行当前模型。";
  $("removeModelCredential").hidden = source !== "session";
  $("modelCredentialValue").placeholder = configured
    ? "输入新的 API Key 以覆盖当前凭证"
    : "输入新的 API Key";
}

function showModelCredentialError(title, message) {
  $("modelCredentialError").hidden = false;
  $("modelCredentialErrorTitle").textContent = title;
  $("modelCredentialErrorMessage").textContent = message;
}

async function openModelCredential(resourceId) {
  const resource = resourceById(resourceId);
  if (!resource || resource.kind !== "model") {
    showToast("模型配置不存在", "请刷新资源列表后重试。", "error");
    return;
  }
  const reference = modelCredentialReference(resource);
  const name = credentialNameFromReference(reference);
  if (!name) {
    showToast("凭证类型暂不支持", "当前 WebUI 仅支持 env:// 模型凭证引用。", "error");
    return;
  }
  state.activeModelResourceId = resource.resourceId;
  $("modelCredentialDisplayName").textContent = resource.displayName;
  $("modelCredentialProvider").textContent = resource.contract?.provider || "openai-compatible";
  $("modelCredentialEndpoint").textContent = resource.contract?.endpointUrl || resource.contract?.baseUrl || "-";
  $("modelCredentialReference").textContent = reference;
  $("modelCredentialValue").value = "";
  $("modelCredentialError").hidden = true;
  const status = await api(`/credentials/${encodeURIComponent(name)}`);
  state.credentialStatuses[reference] = status;
  renderModelCredentialStatus(status);
  openOverlay("modelCredentialOverlay");
}

async function saveModelCredential({ testConnection = false } = {}) {
  const resource = resourceById(state.activeModelResourceId);
  const reference = modelCredentialReference(resource);
  const name = credentialNameFromReference(reference);
  const value = $("modelCredentialValue").value;
  const existing = state.credentialStatuses[reference];
  if (!resource || !name) return;
  if (!value && !existing?.configured) {
    showModelCredentialError("请输入 API Key", "当前模型还没有可用凭证。");
    $("modelCredentialValue").focus();
    return;
  }
  const button = testConnection
    ? $("saveAndTestModelCredential")
    : $("saveModelCredential");
  $("modelCredentialError").hidden = true;
  setButtonLoading(button, true, testConnection ? "正在测试" : "正在保存");
  let saved = false;
  try {
    let status = existing;
    if (value) {
      status = await api(`/credentials/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: { value, persistence: "session" }
      });
      saved = true;
      $("modelCredentialValue").value = "";
      state.credentialStatuses[reference] = status;
      renderModelCredentialStatus(status);
      renderResources();
      populateModelSelect();
    }
    let result = null;
    if (testConnection) {
      result = await api(`/model-profiles/${encodeURIComponent(resource.resourceId)}:test`, {
        method: "POST"
      });
    }
    closeOverlay("modelCredentialOverlay");
    if (state.lastFailedMessage && state.view === "chat") {
      $("chatInput").value = state.lastFailedMessage;
      autoSizeComposer();
      $("chatInput").focus();
    }
    showToast(
      testConnection ? "模型连接测试通过" : "模型凭证已保存",
      testConnection
        ? `${resource.displayName} · ${result?.latencyMs || 0} ms`
        : "凭证已在当前 Studio 会话中生效。"
    );
  } catch (error) {
    showModelCredentialError(
      saved ? "凭证已保存，但连接测试失败" : "模型凭证配置失败",
      error.message
    );
  } finally {
    setButtonLoading(button, false);
  }
}

async function removeModelCredential() {
  const resource = resourceById(state.activeModelResourceId);
  const reference = modelCredentialReference(resource);
  const name = credentialNameFromReference(reference);
  if (!resource || !name) return;
  const button = $("removeModelCredential");
  setButtonLoading(button, true, "正在清除");
  try {
    const status = await api(`/credentials/${encodeURIComponent(name)}`, {
      method: "DELETE"
    });
    state.credentialStatuses[reference] = status;
    renderModelCredentialStatus(status);
    renderResources();
    populateModelSelect();
    showToast("会话凭证已清除", resource.displayName);
  } catch (error) {
    showModelCredentialError("凭证清除失败", error.message);
  } finally {
    setButtonLoading(button, false);
  }
}

async function probeMcp(resourceId) {
  try {
    const resource = await api(`/catalog/mcp-servers/${encodeURIComponent(resourceId)}:probe?timeoutSeconds=15`, {
      method: "POST"
    });
    await refreshCatalog();
    showToast("MCP 探测完成", `已发现 ${resource.health?.toolCount || 0} 个 Tool。`);
  } catch (error) {
    await refreshCatalog();
    showToast("MCP 探测失败", error.message, "error");
  }
}

function addResource() {
  if (state.resourceKind === "model") {
    const model = state.catalog.model[0];
    if (model) openModelCredential(model.resourceId).catch(handleGlobalError);
    return;
  }
  if (state.resourceKind === "mcp") {
    openOverlay("mcpOverlay");
    return;
  }
  if (state.resourceKind === "skill") {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".zip,application/zip";
    input.onchange = async () => {
      if (!input.files?.[0]) return;
      const body = new FormData();
      body.append("file", input.files[0]);
      try {
        await api("/catalog/skills:import", { method: "POST", body });
        await refreshCatalog();
        showToast("Skill 已安装", input.files[0].name);
      } catch (error) {
        showToast("Skill 安装失败", error.message, "error");
      }
    };
    input.click();
    return;
  }
  showToast("资源创建入口正在收敛", "当前可在 Agent 创建流程中选择已有资源。");
}

function renderRunRows() {
  $("runEmpty").hidden = state.runs.length > 0;
  $("runRows").innerHTML = [...state.runs].reverse().map(run => `
    <tr>
      <td><div class="agent-cell-copy"><strong>${escapeHtml(run.agentId)}</strong><span>${escapeHtml(run.id)}</span></div></td>
      <td><span class="mono">${escapeHtml(shortId(run.sessionId, 20))}</span></td>
      <td><span class="status-badge ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span></td>
      <td>${run.durationMs ?? "-"} ms</td>
      <td>${run.usage?.totalTokens || 0}</td>
      <td class="actions-column"><button class="button tertiary small" data-open-run="${escapeHtml(run.id)}" type="button">查看 Trace</button></td>
    </tr>
  `).join("");
}

async function openRun(runId) {
  const run = await api(`/runs/${encodeURIComponent(runId)}`);
  const agent = state.agents.find(item => item.metadata.id === run.agentId);
  if (agent) await openChat(agent.metadata.id);
  state.chatSessionId = run.sessionId;
  state.activeRun = run;
  renderSessionList();
  renderMessages();
  await renderTrace(run);
}

function updatePromptCounter() {
  const length = $("agentPrompt").value.length;
  $("promptCounter").textContent = `${length} / 32768`;
}

function bindEvents() {
  $("createAgentButton").onclick = openCreate;
  $("emptyCreateAgent").onclick = openCreate;
  $("exitCreate").onclick = () => switchView("agents");
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
  $("detailBuild").onclick = () => buildCurrentAgent();
  $("detailChat").onclick = () => openChat().catch(handleGlobalError);
  $("detailInvoke").onclick = openInvocation;
  $("buildCurrentAgent").onclick = () => buildCurrentAgent();
  $("conversationInvoke").onclick = openInvocation;
  $("toggleInspector").onclick = () => $("runInspector").classList.toggle("open");
  $("newSession").onclick = newChatSession;
  $("sendMessage").onclick = () => sendChatMessage();
  $("chatInput").oninput = autoSizeComposer;
  $("chatInput").onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendChatMessage();
    }
  };
  $("copyInvocation").onclick = () => copyInvocation();
  $("addResourceButton").onclick = addResource;
  $("resourceSearch").oninput = renderResources;
  $("resourceStatusFilter").onchange = renderResources;
  $("refreshRuns").onclick = () => refreshRuns().catch(handleGlobalError);

  document.addEventListener("click", event => {
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
        if (view === "observability") refreshRuns().catch(handleGlobalError);
        switchView(view);
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
    const chatAgent = event.target.closest("[data-chat-agent]");
    if (chatAgent) {
      openChat(chatAgent.dataset.chatAgent).catch(handleGlobalError);
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
    const session = event.target.closest("[data-session-id]");
    if (session) {
      state.chatSessionId = session.dataset.sessionId;
      renderSessionList();
      renderMessages();
      const latest = agentRuns().filter(run => run.sessionId === state.chatSessionId).at(-1);
      if (latest) renderTrace(latest).catch(handleGlobalError);
      renderInvocation();
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
    const run = event.target.closest("[data-open-run]");
    if (run) openRun(run.dataset.openRun).catch(handleGlobalError);
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
    state.build = state.current.builds.find(item => item.status === "SUCCEEDED") || null;
  }
  if (state.view === "agent-detail") renderAgentDetail();
  if (state.view === "chat") {
    renderChatAgent();
    renderSessionList();
    renderMessages();
  }
}

function handleGlobalError(error) {
  console.error(error);
  showToast("操作失败", error.message || "未知错误", "error");
}

async function initialize() {
  injectIcons();
  bindEvents();
  updateMcpTransport();
  setRuntimeStatus("Connecting");
  try {
    await establishSession();
    await Promise.all([refreshCatalog(), refreshAgents(), refreshRuns()]);
    resetWizard();
    renderResources();
    renderRunRows();
    switchView("agents");
  } catch (error) {
    setRuntimeStatus("Disconnected");
    handleGlobalError(error);
  }
}

document.addEventListener("DOMContentLoaded", initialize);
