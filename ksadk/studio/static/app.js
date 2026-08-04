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
  "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  "shield-check": '<path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3Z"/><path d="m9 12 2 2 4-4"/>',
  "sparkles": '<path d="m12 3-1.9 5.1L5 10l5.1 1.9L12 17l1.9-5.1L19 10l-5.1-1.9Z"/><path d="M5 3v4"/><path d="M3 5h4"/><path d="M19 17v4"/><path d="M17 19h4"/>',
  "square-pen": '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"/>',
  "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/>',
  "upload": '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
  "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5-5l2.1 2.1-2.8 2.8-2.1-2.1a4 4 0 0 0 5 5l8.5 8.5a2 2 0 0 1-2.8 2.8Z"/>',
  "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'
  ,"zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9Z"/>'
};

const state = {
  csrf: "",
  sessionToken: "",
  bootstrap: null,
  uiCapabilities: null,
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
  },
  chatSessionId: null,
  editingAgentId: null,
  pendingDeleteAgentId: null,
  pendingDeleteSessionId: null,
  activeRun: null,
  recoveringRunIds: new Set(),
  chatViewRevision: 0,
  chatModels: [],
  activeChatModel: "",
  activeModelResourceId: null,
  lastFailedMessage: "",
  invocationTab: "curl",
  traces: [],
  activeTrace: null,
  activeSpanId: null,
  traceTab: "summary",
  traceRawOtlp: null,
  traceDetailExpanded: false,
  authoringMode: "quick",
  authoringConversation: [],
  authoringProposal: null,
  importInspection: null,
  projectInspection: null,
  skillDiscovery: null
};

const terminalOperationStatuses = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "INTERRUPTED"
]);

const $ = id => document.getElementById(id);
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let browserSessionRecovery = null;

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

function formatDuration(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "未上报";
  const milliseconds = Number(value);
  if (milliseconds < 1) return `${milliseconds.toFixed(3)} ms`;
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1)} s`;
}

function formatTokenCount(value) {
  if (value === null || value === undefined) return "未上报";
  return new Intl.NumberFormat("zh-CN").format(Number(value));
}

function formatNanoseconds(value) {
  const milliseconds = Number(BigInt(String(value || "0")) / 1000000n);
  return milliseconds ? new Date(milliseconds).toISOString() : "-";
}

function initialBrowserRoute() {
  const params = new URLSearchParams(location.search);
  return {
    view: params.get("view") || "",
    agentId: params.get("agentId") || "",
    sessionId: params.get("sessionId") || "",
    traceId: params.get("traceId") || ""
  };
}

function syncBrowserRoute() {
  const url = new URL(location.href);
  const currentAgentId = state.current?.draft?.metadata?.id;
  if (currentAgentId) url.searchParams.set("agentId", currentAgentId);
  else url.searchParams.delete("agentId");
  if (state.view === "chat" && state.current?.draft?.metadata?.id) {
    url.searchParams.set("view", "chat");
    if (state.chatSessionId) url.searchParams.set("sessionId", state.chatSessionId);
    else url.searchParams.delete("sessionId");
    url.searchParams.delete("traceId");
  } else if (state.view === "observability") {
    url.searchParams.set("view", "observability");
    url.searchParams.delete("sessionId");
    if (state.activeTrace?.traceId) {
      url.searchParams.set("traceId", state.activeTrace.traceId);
    } else {
      url.searchParams.delete("traceId");
    }
  } else {
    url.searchParams.delete("view");
    url.searchParams.delete("sessionId");
    url.searchParams.delete("traceId");
  }
  history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
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
  state.uiCapabilities = window.AgentKitSharedWeb?.normalizeCapabilities({
    Data: {
      Agent: { Framework: "runtime-adapter" },
      ApiFormats: ["responses", "chat_completions"],
      Capabilities: {
        Thinking: true,
        HostedChat: { Enabled: Boolean(state.bootstrap.features?.run) },
        RunLifecycle: {
          Enabled: Boolean(state.bootstrap.features?.run),
          Resume: true,
          Abort: true,
          Checkpoints: false
        }
      }
    }
  }) || null;
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

async function recoverBrowserSession() {
  if (!browserSessionRecovery) {
    browserSessionRecovery = (async () => {
      const rootResponse = await fetch("/", {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin"
      });
      if (!rootResponse.ok) {
        throw new Error("无法重新连接本地 Studio 服务。");
      }
      const bootstrapResponse = await fetch("/api/v1/system/bootstrap", {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin"
      });
      if (!bootstrapResponse.ok) {
        throw new Error("本地 Studio 会话恢复失败，请刷新页面。");
      }
      state.bootstrap = await bootstrapResponse.json();
      state.csrf = state.bootstrap.csrfToken || "";
      state.sessionToken = "";
      setRuntimeStatus("Ready");
    })().finally(() => {
      browserSessionRecovery = null;
    });
  }
  return browserSessionRecovery;
}

async function api(path, options = {}) {
  const allowSessionRecovery = options.allowSessionRecovery !== false;
  const requestOptions = { ...options };
  delete requestOptions.allowSessionRecovery;
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
    ...requestOptions,
    method,
    headers,
    body,
    credentials: "same-origin"
  });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const error = payload?.error || {};
    if (
      allowSessionRecovery
      && response.status === 401
      && error.code === "LOCAL_SESSION_REQUIRED"
    ) {
      await recoverBrowserSession();
      return api(path, { ...options, allowSessionRecovery: false });
    }
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
  syncBrowserRoute();
}
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
async function openAgentDetail(agentId) {
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  state.current = detail;
  state.build = currentSuccessfulBuild(detail);
  renderGlobalContext();
  renderAgentDetail();
  switchView("agent-detail", { parent: "Agent", title: detail.draft.metadata.name });
}

function renderAgentDetail() {
  if (!state.current) return;
  const draft = state.current.draft;
  const bindings = draft.spec.bindings || {};
  $("detailEdit").hidden = false;
  $("detailAgentName").textContent = draft.metadata.name;
  $("detailAgentMeta").textContent = `${draft.metadata.id} · revision ${draft.metadata.revision}`;
  $("detailSystemPrompt").textContent = draft.spec.instructions.system;
  $("detailTaskPrompt").textContent = draft.spec.instructions.task || "未配置任务契约";
  const manifestModels = String(
    draft.metadata.labels?.["agentkit.ksyun.com/models"] || ""
  ).split(",").map(item => item.trim()).filter(Boolean);
  const boundModelIds = bindings.modelProfileIds?.length
    ? bindings.modelProfileIds
    : bindings.modelProfileId ? [bindings.modelProfileId] : [];
  const groups = [
    ["Model", boundModelIds.length
      ? boundModelIds
      : manifestModels.length ? manifestModels : draft.metadata.labels?.["agentkit.ksyun.com/model"]
        ? [draft.metadata.labels["agentkit.ksyun.com/model"]]
        : []],
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
    <div><dt>Runtime</dt><dd>${escapeHtml(draft.spec.runtime?.type || draft.metadata.labels?.["agentkit.ksyun.com/framework"] || "adk")}</dd></div>
    <div><dt>策略</dt><dd>${escapeHtml(draft.spec.execution.strategy)}</dd></div>
    <div><dt>最大步骤</dt><dd>${draft.spec.execution.maxSteps}</dd></div>
    <div><dt>超时</dt><dd>${draft.spec.execution.timeoutSeconds}s</dd></div>
  `;
  const latestBuild = currentSuccessfulBuild(state.current);
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
    const completedBuild = await api(`/builds/${encodeURIComponent(completed.resourceId)}`);
    const refreshed = await api(`/agents/${encodeURIComponent(draft.metadata.id)}`);
    state.agentDetails.set(draft.metadata.id, refreshed);
    renderAgentRows();
    if (state.current?.draft?.metadata?.id === draft.metadata.id) {
      state.current = refreshed;
      state.build = completedBuild;
      renderBuildWorkspace();
    }
    showToast(
      `${draft.spec.runtime?.type || "Agent"} Bundle 构建完成`,
      shortId(completedBuild.bundleDigest, 36)
    );
    return completedBuild;
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
  const build = state.build || currentSuccessfulBuild(state.current);
  $("buildAgentName").textContent = draft?.metadata.name || "未选择";
  $("buildRevision").textContent = draft ? `r${draft.metadata.revision}` : "-";
  $("buildDigest").textContent = build?.bundleDigest || "-";
  setStatusBadge($("buildStatus"), build?.status || "IDLE");
  $("runtimeManifestDigest").textContent = build?.manifestSha256 || build?.sourceDigest || build?.resolvedDigest || "-";
  $("runtimeContract").textContent = build?.runtimeName
    ? `${build.runtimeName} ${build.runtimeVersion || ""}`.trim()
    : build?.runtimeType || draft?.spec?.runtime?.type || "未选择";
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

async function refreshChatModels(agentId) {
  const payload = await api(`/agents/${encodeURIComponent(agentId)}/models`);
  state.chatModels = payload.Models || [];
  const allowedIds = state.chatModels.map(item => String(item.id));
  const selected = allowedIds.includes(state.activeChatModel)
    ? state.activeChatModel
    : String(payload.Current || allowedIds[0] || "");
  state.activeChatModel = selected;
  $("chatModel").innerHTML = state.chatModels.length
    ? state.chatModels.map(item => `
      <option value="${escapeHtml(item.id)}">${escapeHtml(item.display_name || item.displayName || item.id)}</option>
    `).join("")
    : '<option value="">未绑定模型</option>';
  $("chatModel").value = selected;
  $("chatModel").disabled = state.chatModels.length <= 1;
}

async function openChat(
  agentId = state.current?.draft?.metadata?.id,
  { sessionId = null } = {}
) {
  if (!agentId) {
    const first = state.agents[0];
    if (!first) {
      openCreate();
      return;
    }
    agentId = first.metadata.id;
  }
  const viewRevision = ++state.chatViewRevision;
  const detail = await api(`/agents/${encodeURIComponent(agentId)}`);
  if (viewRevision !== state.chatViewRevision) return;
  state.current = detail;
  state.build = currentSuccessfulBuild(detail);
  clearChatRunInspector();
  renderGlobalContext();
  state.chatSessionId = sessionId;
  $("sendMessage").disabled = false;
  renderChatAgent();
  switchView("chat");
  await Promise.all([refreshRuns(), refreshChatModels(agentId)]);
  renderChatAgent();
  const requestedSessionExists = state.chatSessionId
    && groupedSessions().some(item => item.sessionId === state.chatSessionId);
  if (!requestedSessionExists) {
    state.chatSessionId = groupedSessions()[0]?.sessionId || null;
    renderSessionList();
  }
  renderMessages();
  const latest = agentRuns()
    .filter(run => run.sessionId === state.chatSessionId)
    .at(-1);
  if (latest) {
    state.activeRun = latest;
    await renderTrace(latest);
  }
  renderInvocation();
  syncBrowserRoute();
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
      <div><dt>Model</dt><dd>${escapeHtml(state.chatModels.length > 1 ? `${state.chatModels.length} 个已绑定` : draft.metadata.labels?.["agentkit.ksyun.com/model"] || resourceById(bindings.modelProfileId)?.displayName || "未选择")}</dd></div>
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
  recoverActiveRuns();
}

function recoverActiveRuns() {
  state.runs
    .filter(run => run.status === "RUNNING" && !state.recoveringRunIds.has(run.id))
    .forEach(run => pollActiveRun(run.id));
}

async function pollActiveRun(runId) {
  state.recoveringRunIds.add(runId);
  try {
    while (true) {
      const run = await api(`/runs/${encodeURIComponent(runId)}`);
      const index = state.runs.findIndex(item => item.id === run.id);
      if (index >= 0) state.runs[index] = run;
      else state.runs.push(run);
      const visible = state.current?.draft?.metadata?.id === run.agentId
        && state.chatSessionId === run.sessionId;
      if (visible) {
        state.activeRun = run;
        renderSessionList();
        renderMessages();
        setStatusBadge($("inspectorStatus"), run.status);
      }
      if (run.status !== "RUNNING") {
        if (visible) await renderTrace(run);
        if (state.view === "observability") await refreshTraces();
        break;
      }
      await sleep(400);
    }
  } catch (error) {
    console.warn("恢复本地 Run 状态失败", runId, error);
  } finally {
    state.recoveringRunIds.delete(runId);
  }
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
        <div class="session-item ${state.chatSessionId === sessionId ? "active" : ""}">
          <button class="session-main" data-session-id="${escapeHtml(sessionId)}" title="${escapeHtml(firstInput)}" type="button">
            <span class="session-status ${latest?.status === "RUNNING" ? "running" : ""}"></span>
            <strong>${escapeHtml(shortId(firstInput, 28))}</strong>
          </button>
          <time>${formatSessionTime(latest?.startedAt || latest?.completedAt)}</time>
          <button class="session-more" data-session-menu="${escapeHtml(sessionId)}" type="button" aria-label="会话操作" title="会话操作">•••</button>
          <div class="session-menu" data-session-menu-popover="${escapeHtml(sessionId)}" hidden>
            <button data-delete-session="${escapeHtml(sessionId)}" type="button">删除会话</button>
          </div>
        </div>
      `;
    }).join("")
    : '<div class="session-empty">当前 Agent 还没有会话</div>';
}

function formatSessionTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
}

function openDeleteSession(sessionId) {
  state.pendingDeleteSessionId = sessionId;
  document.querySelectorAll("[data-session-menu-popover]").forEach(node => { node.hidden = true; });
  openOverlay("deleteSessionOverlay");
}

async function deleteSession() {
  const sessionId = state.pendingDeleteSessionId;
  if (!sessionId) return;
  const button = $("confirmDeleteSession");
  setButtonLoading(button, true, "正在删除");
  try {
    await api(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    if (state.chatSessionId === sessionId) {
      state.chatSessionId = null;
      state.activeRun = null;
    }
    state.pendingDeleteSessionId = null;
    closeOverlay("deleteSessionOverlay");
    await refreshRuns();
    if (!state.chatSessionId) {
      state.chatSessionId = groupedSessions()[0]?.sessionId || null;
    }
    renderMessages();
    renderInvocation();
    syncBrowserRoute();
    showToast("会话已删除", "相关 Run 与 Trace 已从本地工作区移除。");
  } catch (error) {
    showToast("删除失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function openDeleteAgent(agentId) {
  state.pendingDeleteAgentId = agentId;
  $("deleteAgentTitle").textContent = `删除 ${agentId}？`;
  $("deleteAgentDescription").textContent = "该 Agent 的 YAML、Build、Artifact、Run 与 Trace 将移入 .agentkit/trash，可从工作区手工恢复。";
  openOverlay("deleteAgentOverlay");
}

async function deleteAgent() {
  const agentId = state.pendingDeleteAgentId;
  if (!agentId) return;
  const button = $("confirmDeleteAgent");
  setButtonLoading(button, true, "正在删除");
  try {
    await api(`/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
    const deletingCurrent = state.current?.draft?.metadata?.id === agentId;
    if (deletingCurrent) {
      state.current = null;
      state.build = null;
      state.chatSessionId = null;
      state.activeRun = null;
      state.chatModels = [];
      state.activeChatModel = "";
      state.editingAgentId = null;
    }
    state.pendingDeleteAgentId = null;
    closeOverlay("deleteAgentOverlay");
    await Promise.all([refreshAgents(), refreshRuns()]);
    switchView("agents");
    showToast(
      "Agent 已删除",
      "YAML 与关联的本地构建、运行和 Trace 已移入回收站。"
    );
  } catch (error) {
    showToast("删除失败", error.message, "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function renderMessages() {
  const runs = state.chatSessionId
    ? agentRuns().filter(run => run.sessionId === state.chatSessionId)
    : [];
  $("chatEmpty").hidden = runs.length > 0;
  document.querySelectorAll("#messageList .message").forEach(node => node.remove());
  runs.forEach(run => {
    appendMessage("user", run.input, { time: run.startedAt });
    if (run.status === "RUNNING") {
      appendMessage("assistant", "", {
        loading: true,
        runId: run.id,
        model: run.model,
        time: run.startedAt
      });
      return;
    }
    const cancelled = run.status === "CANCELLED";
    appendMessage(
      run.status === "COMPLETED" ? "assistant" : cancelled ? "status" : "error",
      run.output || run.error?.message || (cancelled ? "运行已取消" : `运行状态：${run.status}`),
      {
        runId: run.id,
        model: run.model,
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
    retryRunId = "",
    model = ""
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
  const renderedContent = loading
    ? '<span class="message-loading"><i></i><i></i><i></i></span>'
    : role === "assistant"
      ? '<ksadk-message></ksadk-message>'
      : `<span class="plain-message">${escapeHtml(content)}</span>`;
  node.innerHTML = `
    <div class="message-meta"><strong>${escapeHtml(author)}</strong><span>${time ? formatDate(time) : "刚刚"}</span>${model ? `<span class="message-model">${escapeHtml(model)}</span>` : ""}${runId ? `<span>${escapeHtml(shortId(runId, 18))}</span>` : ""}</div>
    <div class="message-content">${renderedContent}${recovery}</div>
  `;
  const markdown = node.querySelector("ksadk-message");
  if (markdown) {
    if (customElements.get("ksadk-message")) markdown.content = content;
    else customElements.whenDefined("ksadk-message").then(() => { markdown.content = content; });
  }
  $("messageList").append(node);
  return node;
}

function updateStreamingMessage(node, content) {
  node.removeAttribute("data-loading-message");
  const container = node.querySelector(".message-content");
  let markdown = container.querySelector("ksadk-message");
  if (!markdown) {
    container.replaceChildren(document.createElement("ksadk-message"));
    markdown = container.querySelector("ksadk-message");
  }
  if (customElements.get("ksadk-message")) markdown.content = content;
  else customElements.whenDefined("ksadk-message").then(() => { markdown.content = content; });
  scrollMessages();
}

function parseSseBlock(block) {
  let id = "";
  let type = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  try {
    return { id, type, data: JSON.parse(raw) };
  } catch {
    return { id, type, data: { text: raw } };
  }
}

async function streamRun(buildId, body, onEvent, allowSessionRecovery = true) {
  const headers = new Headers({
    "Content-Type": "application/json",
    "Idempotency-Key": operationKey("chat-stream")
  });
  if (state.csrf) headers.set("X-CSRF-Token", state.csrf);
  if (state.sessionToken) headers.set("X-AgentKit-Session", state.sessionToken);
  const response = await fetch(`/api/v1/builds/${encodeURIComponent(buildId)}/run:stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    credentials: "same-origin"
  });
  if (response.status === 401 && allowSessionRecovery) {
    await recoverBrowserSession();
    return streamRun(buildId, body, onEvent, false);
  }
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => ({}));
    const error = payload?.error || {};
    const failure = new Error(error.message || `运行请求失败（HTTP ${response.status}）`);
    failure.code = error.code || "";
    throw failure;
  }
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const event = parseSseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event) onEvent(event);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  const tail = parseSseBlock(buffer);
  if (tail) onEvent(tail);
}

function scrollMessages() {
  $("messageList").scrollTop = $("messageList").scrollHeight;
}

function autoSizeComposer() {
  const input = $("chatInput");
  input.style.height = "42px";
  input.style.height = `${Math.min(Math.max(input.scrollHeight, 42), 160)}px`;
}

async function ensureChatBuild() {
  if (state.build?.status === "SUCCEEDED") return state.build;
  const latest = currentSuccessfulBuild(state.current);
  if (latest) {
    state.build = latest;
    return latest;
  }
  showToast(
    "正在准备不可变 Runtime Bundle",
    "第一次对话前需要完成本地构建。"
  );
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
  const runAgentId = state.current.draft.metadata.id;
  const selectedModel = $("chatModel").value || state.activeChatModel;
  const chatRevision = state.chatViewRevision;
  const requestedSessionId = state.chatSessionId;
  const isVisible = () => state.chatViewRevision === chatRevision
    && state.current?.draft?.metadata?.id === runAgentId;
  $("sendMessage").disabled = true;
  input.value = "";
  autoSizeComposer();
  appendMessage("user", content);
  const loading = appendMessage("assistant", "", {
    loading: true,
    model: selectedModel
  });
  scrollMessages();
  try {
    const build = await ensureChatBuild();
    if (!build) {
      throw new Error("Runtime Bundle 尚未就绪");
    }
    const body = {
      model: selectedModel,
      input: { role: "user", content },
      environment: "local",
      stream: true
    };
    if (requestedSessionId) body.sessionId = requestedSessionId;
    beginChatRunInspector();
    const liveEvents = [];
    let streamedOutput = "";
    let streamedSessionId = requestedSessionId;
    let streamFailure = null;
    await streamRun(build.id, body, event => {
      liveEvents.push(event);
      if (event.type === "run.created") streamedSessionId = event.data.sessionId || streamedSessionId;
      if (event.type === "message.delta") {
        streamedOutput += String(event.data.text || "");
        if (isVisible()) updateStreamingMessage(loading, streamedOutput);
      } else if (event.type === "message.completed") {
        streamedOutput = String(event.data.text || streamedOutput);
        if (isVisible()) updateStreamingMessage(loading, streamedOutput);
      } else if (event.type === "run.failed") {
        streamFailure = event.data;
      }
      if (isVisible()) renderTimeline(liveEvents);
    });
    if (streamFailure) {
      const failure = new Error(streamFailure.message || streamFailure.error || "Agent 运行失败");
      failure.code = streamFailure.code || "";
      throw failure;
    }
    await refreshRuns();
    const run = state.runs
      .filter(item => item.agentId === runAgentId && item.sessionId === streamedSessionId)
      .at(-1);
    if (!run) throw new Error("运行已结束，但未找到对应的 Run 记录");
    loading.remove();
    const completedSuccessfully = run.status === "COMPLETED";
    if (run.error?.code === "SECRET_NOT_FOUND") {
      state.lastFailedMessage = content;
    }
    if (isVisible()) {
      state.chatSessionId = streamedSessionId;
      syncBrowserRoute();
      state.activeRun = run;
      renderSessionList();
      renderMessages();
      await renderTrace(run);
      renderInvocation();
      if (!completedSuccessfully && run.status !== "CANCELLED") {
        showToast(
          "Agent 运行未完成",
          run.error?.message || `运行状态：${run.status}`,
          "error"
        );
      }
    }
  } catch (error) {
    loading.remove();
    if (error.code === "SECRET_NOT_FOUND") {
      state.lastFailedMessage = content;
    }
    if (isVisible()) {
      appendMessage("error", error.message, { errorCode: error.code || "" });
      setStatusBadge($("inspectorStatus"), "FAILED");
      showToast("运行失败", error.message, "error");
    }
  } finally {
    if (isVisible()) {
      $("sendMessage").disabled = false;
      input.focus();
      scrollMessages();
    }
  }
}

async function renderTrace(run) {
  const trace = await api(`/traces/${encodeURIComponent(run.traceId)}`);
  renderSpanTimeline(trace.spans || []);
  $("inspectorRunId").textContent = run.id;
  setStatusBadge($("inspectorStatus"), run.status);
  $("usageInput").textContent = trace.metrics?.usageReported
    ? formatTokenCount(trace.metrics.inputTokens)
    : "未上报";
  $("usageOutput").textContent = trace.metrics?.usageReported
    ? formatTokenCount(trace.metrics.outputTokens)
    : "未上报";
  $("usageDuration").textContent = formatDuration(trace.metrics?.durationMs);
  $("openFullTrace").disabled = false;
  $("openFullTrace").dataset.traceId = trace.traceId;
}

function renderSpanTimeline(spans) {
  const ordered = [...spans].sort((left, right) => (
    Number(BigInt(left.startTimeUnixNano || "0") - BigInt(right.startTimeUnixNano || "0"))
  ));
  $("eventTimeline").innerHTML = ordered.length
    ? ordered.map(span => `
      <div class="timeline-event">
        <span class="timeline-marker"></span>
        <div class="timeline-copy">
          <strong>${escapeHtml(span.name)}</strong>
          <span>${escapeHtml(span.kind)} · ${escapeHtml(formatDuration(span.durationMs))} · ${escapeHtml(span.status)}</span>
        </div>
      </div>
    `).join("")
    : '<div class="timeline-empty">当前 Trace 没有 Span</div>';
}

function renderTimeline(events) {
  const visibleEvents = compactTimelineEvents(events);
  $("eventTimeline").innerHTML = visibleEvents.length
    ? visibleEvents.map(event => `
      <div class="timeline-event">
        <span class="timeline-marker"></span>
        <div class="timeline-copy"><strong>${escapeHtml(event.type)}</strong><span>${escapeHtml(eventSummary(event))}</span></div>
      </div>
    `).join("")
    : '<div class="timeline-empty">发送消息后显示模型和 Tool 事件</div>';
}

function clearChatRunInspector() {
  state.activeRun = null;
  $("eventTimeline").innerHTML = '<div class="timeline-empty">发送消息后显示模型和 Tool 事件</div>';
  setStatusBadge($("inspectorStatus"), "IDLE");
  $("inspectorRunId").textContent = "尚未运行";
  $("usageInput").textContent = "未上报";
  $("usageOutput").textContent = "未上报";
  $("usageDuration").textContent = "未上报";
  $("openFullTrace").disabled = true;
  delete $("openFullTrace").dataset.traceId;
}

function beginChatRunInspector() {
  clearChatRunInspector();
  setStatusBadge($("inspectorStatus"), "RUNNING");
  $("inspectorRunId").textContent = "正在创建 Run";
  $("eventTimeline").innerHTML = '<div class="timeline-empty">正在建立 Runtime 连接…</div>';
}

function compactTimelineEvents(events) {
  const compacted = [];
  for (const event of events || []) {
    if (!["thinking.delta", "message.delta"].includes(event?.type)) {
      compacted.push(event);
      continue;
    }
    const previous = compacted.at(-1);
    if (previous?.type === event.type) {
      previous.data.text = String(previous.data.text || "") + String(event.data?.text || "");
      previous.createdAt = event.createdAt || previous.createdAt;
    } else {
      compacted.push({ ...event, data: { ...(event.data || {}), text: String(event.data?.text || "") } });
    }
  }
  return compacted;
}

function eventSummary(event) {
  const data = event.data || {};
  if (event.type === "run.started") return `${data.runtime || data.runtimeType || "RuntimeAdapter"}`;
  if (event.type === "command.started") return data.command || "command";
  if (event.type === "command.completed") return `exit ${data.exitCode ?? "-"} · ${data.durationMs || 0} ms`;
  if (["thinking.delta", "message.delta"].includes(event.type)) return String(data.text || "").trim();
  if (data.model) return `${data.model} · step ${data.step || 1}`;
  if (data.tool) return data.tool;
  if (data.usage) return `${data.usage.totalTokens || 0} tokens`;
  if (data.output) return shortId(data.output, 60);
  return formatDate(event.createdAt);
}

function newChatSession() {
  state.chatViewRevision += 1;
  state.chatSessionId = null;
  $("sendMessage").disabled = false;
  renderSessionList();
  renderMessages();
  clearChatRunInspector();
  $("chatInput").focus();
  renderInvocation();
  syncBrowserRoute();
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
  const buildId = state.build?.id || currentSuccessfulBuild(state.current)?.id;
  const sessionId = state.chatSessionId;
  const endpoint = "/v1/responses";
  $("invokeEndpoint").textContent = `POST ${endpoint}`;
  $("invokeBuildId").textContent = buildId || "尚未构建";
  $("invokeSessionId").textContent = sessionId || "首次调用可省略";
  const template = state.current?.draft?.metadata?.labels?.["agentkit.ksyun.com/template"] || "blank";
  const modelResource = resourceById(
    state.current?.draft?.spec?.bindings?.modelProfileId || ""
  );
  const body = {
    model: $("chatModel").value
      || state.activeChatModel
      || modelResource?.contract?.model
      || state.current?.draft?.metadata?.labels?.["agentkit.ksyun.com/model"]
      || "glm-5.1",
    input: [{
      role: "user",
      content: [{
        type: "input_text",
        text: template === "research"
          ? "调研 Agent 工程平台的核心能力"
          : "请根据你的职责处理这个请求"
      }]
    }],
    metadata: {
      agent_id: state.current?.draft?.metadata?.id || "review-helper"
    },
    ...(sessionId ? { conversation: sessionId } : {}),
    stream: true
  };
  const code = state.invocationTab === "curl"
    ? [
      `curl -X POST "${window.location.origin}${endpoint}" \\`,
      '  -H "Content-Type: application/json" \\',
      '  -H "Authorization: Bearer <RUNTIME_API_KEY>" \\',
      `  -d '${JSON.stringify(body, null, 2)}'`
    ].join("\n")
    : [
      `const response = await fetch("${window.location.origin}${endpoint}", {`,
      '  method: "POST",',
      "  headers: {",
      '    "Content-Type": "application/json",',
      '    "Authorization": `Bearer ${runtimeApiKey}`',
      "  },",
      `  body: JSON.stringify(${JSON.stringify(body, null, 2)})`,
      "});",
      "for await (const event of response.body) {",
      "  // OpenAI Responses SSE: created / output_text.delta / completed",
      "  console.log(event);",
      "}"
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
    ? "发现 Skill"
    : state.resourceKind === "tool"
    ? "添加 Python Tool"
    : "添加资源";
  const headings = state.resourceKind === "model"
    ? ["发现来源", "上下文窗口", "输入模态"]
    : state.resourceKind === "tool"
    ? ["来源", "Tool 分组", "权限 / 边界"]
    : ["来源", "版本", "说明"];
  $("resourceSourceHeading").textContent = headings[0];
  $("resourceDetailHeading").textContent = headings[1];
  $("resourceCapabilityHeading").textContent = headings[2];
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
      <td>${resourceSourceMarkup(item)}</td>
      <td>${resourceDetailMarkup(item)}</td>
      <td>${resourceCapabilityMarkup(item)}</td>
      <td>${resourceStatusMarkup(item)}</td>
      <td class="actions-column">${resourceActionMarkup(item)}</td>
    </tr>
  `).join("");
  injectIcons($("resourceRows"));
}

function resourceSourceMarkup(item) {
  const labels = {
    provider: "模型服务 /v1/models",
    builtin: "ksadk 内置",
    local: "工作区自定义",
    market: "市场"
  };
  return escapeHtml(labels[item.source] || item.source);
}

function resourceDetailMarkup(item) {
  if (item.kind === "model") {
    const metadata = item.contract?.metadata || {};
    const tokens = Number(metadata.context_window_tokens || 0);
    const origin = item.contract?.discovery?.contextWindow === "provider"
      ? "服务返回"
      : "ksadk 默认";
    const value = tokens >= 1000000
      ? `${(tokens / 1000000).toFixed(tokens % 1000000 ? 1 : 0)}M`
      : tokens >= 1000
      ? `${Math.round(tokens / 1000)}K`
      : `${tokens || "-"}`;
    return `<strong>${escapeHtml(value)}</strong><span class="resource-origin">${escapeHtml(origin)}</span>`;
  }
  if (item.kind === "tool") {
    return `<span class="status-badge neutral">${escapeHtml(item.contract?.group || item.category || "general")}</span>`;
  }
  return `<span class="mono">${escapeHtml(item.version)}</span>`;
}

function resourceCapabilityMarkup(item) {
  if (item.kind === "model") {
    const metadata = item.contract?.metadata || {};
    const capabilities = metadata.capabilities || {};
    const modalities = ["文字"];
    if (capabilities.multimodal_input_image) modalities.push("图片");
    if (capabilities.multimodal_input_video) modalities.push("视频");
    if (capabilities.multimodal_input_file) modalities.push("文件");
    const origin = item.contract?.discovery?.inputModalities === "provider"
      ? "服务返回"
      : "ksadk 默认";
    return `${escapeHtml(modalities.join(" + "))}<span class="resource-origin">${escapeHtml(origin)}</span>`;
  }
  if (item.kind === "tool") {
    const approval = item.contract?.approval === "always" ? "需审批" : "无需审批";
    const boundary = item.contract?.boundary || "ksadk-runtime";
    return `${escapeHtml(approval)}<span class="resource-origin">${escapeHtml(boundary)}</span>`;
  }
  return escapeHtml(item.description || "未提供说明");
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

function renderSkillDiscovery() {
  const candidates = state.skillDiscovery?.candidates || [];
  $("skillDiscoveryList").innerHTML = candidates.length
    ? candidates.map(candidate => {
      const risk = candidate.risk || {};
      const valid = ["ready", "conflict"].includes(candidate.status);
      const details = candidate.diagnostics?.map(item => item.message).join("；")
        || `${candidate.fileCount || 0} 个文件 · ${formatByteCount(candidate.totalBytes || 0)}`;
      return `<label class="skill-candidate ${valid ? "" : "invalid"}">
        <input type="radio" name="skillCandidate" data-skill-candidate="${escapeHtml(candidate.candidateId)}" ${valid ? "" : "disabled"}>
        <span class="capability-icon"><svg data-icon="sparkles"></svg></span>
        <span class="selection-item-copy"><strong>${escapeHtml(candidate.displayName || candidate.name)}</strong><span>${escapeHtml(candidate.path)} · ${escapeHtml(candidate.version || "版本无效")}</span><small>${escapeHtml(details)}</small></span>
        <span class="status-badge ${candidate.status === "ready" ? "success" : "warning"}">${escapeHtml(candidate.status)}${risk.requiresReview ? " · 需复核" : ""}</span>
      </label>`;
    }).join("")
    : '<div class="trace-stage-empty compact"><p>安全默认目录中没有发现 Skill。</p></div>';
  $("commitDiscoveredSkill").disabled = true;
  injectIcons($("skillDiscoveryOverlay"));
}

function formatByteCount(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

async function discoverWorkspaceSkills() {
  const button = $("discoverSkills");
  $("skillDiscoveryError").hidden = true;
  const scanPaths = $("skillScanPaths").value
    .split(",")
    .map(item => item.trim())
    .filter(Boolean);
  setButtonLoading(button, true, "正在扫描");
  try {
    state.skillDiscovery = await api("/catalog/skills:discover", {
      method: "POST",
      body: { scanPaths }
    });
    renderSkillDiscovery();
  } catch (error) {
    $("skillDiscoveryError").hidden = false;
    $("skillDiscoveryErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

async function commitSelectedSkill(overwrite = false) {
  const selected = document.querySelector("[data-skill-candidate]:checked");
  const token = state.skillDiscovery?.inspectionToken;
  if (!selected || !token) return;
  const candidate = state.skillDiscovery.candidates.find(
    item => item.candidateId === selected.dataset.skillCandidate
  );
  const button = $("commitDiscoveredSkill");
  setButtonLoading(button, true, "正在导入");
  try {
    const created = await api(`/catalog/skills/discoveries/${encodeURIComponent(token)}:commit`, {
      method: "POST",
      body: { candidateId: selected.dataset.skillCandidate, overwrite }
    });
    await refreshCatalog();
    closeOverlay("skillDiscoveryOverlay");
    state.skillDiscovery = null;
    showToast("Skill 已安装", `${created.displayName || created.name} · ${created.version}`);
  } catch (error) {
    if (error.code === "SKILL_IMPORT_CONFLICT" && !overwrite) {
      if (window.confirm(`Skill ${candidate?.displayName || candidate?.name || ""} 已存在，是否覆盖并把旧版本移入回收站？`)) {
        await commitSelectedSkill(true);
      }
      return;
    }
    $("skillDiscoveryError").hidden = false;
    $("skillDiscoveryErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
  }
}

async function savePythonTool(event) {
  event.preventDefault();
  const form = $("pythonToolForm");
  if (!form.reportValidity()) return;
  const button = $("savePythonTool");
  $("pythonToolError").hidden = true;
  setButtonLoading(button, true, "正在保存");
  try {
    const name = $("pythonToolName").value.trim();
    const created = await api("/catalog/tools", {
      method: "POST",
      body: {
        displayName: name,
        category: "custom",
        contract: {
          name,
          version: "1.0.0",
          description: $("pythonToolDescription").value.trim(),
          inputSchema: { type: "object", properties: {} },
          outputSchema: { type: "object", properties: {} },
          executor: "python",
          sourcePath: $("pythonToolSource").value.trim(),
          callableName: $("pythonToolCallable").value.trim(),
          sideEffect: "none",
          approval: "never"
        }
      }
    });
    await refreshCatalog();
    form.reset();
    closeOverlay("pythonToolOverlay");
    showToast("Python Tool 已保存", `${created.displayName} · SHA-256 已锁定`);
  } catch (error) {
    $("pythonToolError").hidden = false;
    $("pythonToolErrorMessage").textContent = error.message;
  } finally {
    setButtonLoading(button, false);
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
    state.skillDiscovery = null;
    $("skillDiscoveryList").innerHTML = '<div class="trace-stage-empty compact"><p>点击扫描候选。</p></div>';
    $("skillDiscoveryError").hidden = true;
    $("commitDiscoveredSkill").disabled = true;
    openOverlay("skillDiscoveryOverlay");
    return;
  }
  if (state.resourceKind === "tool") {
    $("pythonToolError").hidden = true;
    openOverlay("pythonToolOverlay");
    return;
  }
  showToast("资源创建入口正在收敛", "当前可在 Agent 创建流程中选择已有资源。");
}
async function refreshTraces({ selectFirst = true } = {}) {
  const query = new URLSearchParams({ limit: "500" });
  const agentId = $("traceAgentFilter").value;
  const status = $("traceStatusFilter").value;
  if (agentId) query.set("agentId", agentId);
  if (status) query.set("status", status);
  const payload = await api(`/traces?${query}`);
  state.traces = payload.items || [];
  renderTraceList();
  const currentId = state.activeTrace?.traceId;
  const currentStillVisible = currentId
    && state.traces.some(trace => trace.traceId === currentId);
  if (currentStillVisible) {
    await openTrace(currentId, { switchToView: false });
  } else if (selectFirst && state.traces.length) {
    await openTrace(state.traces[0].traceId, { switchToView: false });
  } else {
    clearTraceExplorer();
  }
}

function filteredTraces() {
  const query = $("traceSearch").value.trim().toLowerCase();
  if (!query) return state.traces;
  return state.traces.filter(trace => [
    trace.traceId,
    trace.runId,
    trace.sessionId,
    trace.agentId,
    trace.model,
    trace.runtimeType
  ].some(value => String(value || "").toLowerCase().includes(query)));
}

function renderTraceList() {
  const traces = filteredTraces();
  $("traceCount").textContent = `${traces.length} 条`;
  $("traceEmpty").hidden = traces.length > 0;
  $("traceList").hidden = traces.length === 0;
  $("traceList").innerHTML = traces.map(trace => `
    <button class="trace-list-item ${state.activeTrace?.traceId === trace.traceId ? "active" : ""}"
      data-open-trace="${escapeHtml(trace.traceId)}" type="button">
      <span class="trace-list-status ${escapeHtml(trace.status)}"></span>
      <span class="trace-list-copy">
        <strong>${escapeHtml(trace.agentId || "unknown-agent")}</strong>
        <span>${escapeHtml(shortId(trace.traceId, 24))}</span>
        <span class="trace-list-meta">
          <span>${escapeHtml(formatDate(trace.startedAt))}</span>
          <span>${escapeHtml(formatDuration(trace.durationMs))} · ${trace.usageReported ? `${escapeHtml(formatTokenCount(trace.totalTokens))} tokens` : "Token 未上报"}</span>
        </span>
      </span>
    </button>
  `).join("");
}

async function openTrace(traceId, { switchToView = true } = {}) {
  const trace = await api(`/traces/${encodeURIComponent(traceId)}`);
  // 从会话工作台直达 Trace 时，列表尚未预取。先同步目录，避免详情已经
  // 正确展示而左侧却错误提示“还没有 Trace”。由 refreshTraces 打开的条目
  // 已在 state.traces 中，不会产生重复请求。
  if (!state.traces.some(item => item.traceId === trace.traceId)) {
    const payload = await api("/traces?limit=500");
    state.traces = payload.items || [];
  }
  state.activeTrace = trace;
  state.activeSpanId = trace.rootSpanId || trace.spans?.[0]?.spanId || null;
  state.traceRawOtlp = null;
  $("copyRawOtlp").disabled = false;
  state.traceTab = "summary";
  renderTraceList();
  renderTraceExplorer();
  if (switchToView) switchView("observability");
  else syncBrowserRoute();
}

function clearTraceExplorer() {
  state.activeTrace = null;
  state.activeSpanId = null;
  state.traceRawOtlp = null;
  $("traceMetricStatus").textContent = "未选择";
  $("traceMetricSpanCount").textContent = "选择一条 Trace 查看";
  $("traceMetricDuration").textContent = "未上报";
  $("traceMetricDurationSource").textContent = "等待 Runtime 上报";
  $("traceMetricTokens").textContent = "未上报";
  $("traceMetricTokenSplit").textContent = "输入 / 输出";
  $("traceMetricModel").textContent = "-";
  $("traceMetricRuntime").textContent = "Runtime";
  $("traceTitle").textContent = "选择一条 Trace";
  $("traceIdLabel").textContent = "-";
  $("copyTraceparent").disabled = true;
  $("copyRawOtlp").disabled = true;
  $("traceSpanTree").innerHTML = '<div class="trace-stage-empty"><svg data-icon="network"></svg><p>选择左侧 Trace，查看 Agent、模型和 Tool 的父子关系与耗时。</p></div>';
  $("traceDetailTitle").textContent = "Span 详情";
  $("traceDetailSubtitle").textContent = "尚未选择 Span";
  $("traceDetailContent").innerHTML = '<div class="trace-stage-empty compact"><p>选择一个 Span 查看标准属性。</p></div>';
  $("traceRawOtlp").hidden = true;
  $("traceDetail").querySelector(".trace-detail-body").classList.remove("raw-active");
  injectIcons($("traceExplorer"));
  syncBrowserRoute();
}

function renderTraceExplorer() {
  const trace = state.activeTrace;
  if (!trace) {
    clearTraceExplorer();
    return;
  }
  const metrics = trace.metrics || {};
  $("traceMetricStatus").textContent = trace.status || "UNSET";
  $("traceMetricSpanCount").textContent = `${trace.spans?.length || 0} Span · ${trace.target?.name || "本地工作区"}`;
  $("traceMetricDuration").textContent = formatDuration(metrics.durationMs);
  $("traceMetricDurationSource").textContent = metrics.durationMs === null || metrics.durationMs === undefined
    ? "Runtime 未上报"
    : metrics.durationSource === "runtime" ? "Runtime 精确上报" : "Studio 时钟回退";
  $("traceMetricTokens").textContent = metrics.usageReported
    ? formatTokenCount(metrics.totalTokens)
    : "未上报";
  $("traceMetricTokenSplit").textContent = metrics.usageReported
    ? `${formatTokenCount(metrics.inputTokens)} 输入 · ${formatTokenCount(metrics.outputTokens)} 输出`
    : "Provider / Runtime 未返回 Usage";
  $("traceMetricModel").textContent = trace.model || "-";
  $("traceMetricRuntime").textContent = `${trace.runtimeType || "unknown"} · ${metrics.usageSource || "Usage 未上报"}`;
  $("traceTitle").textContent = `${trace.agentId || "Agent"} · ${trace.runId || "Run"}`;
  $("traceIdLabel").textContent = trace.traceId;
  $("copyTraceparent").disabled = false;
  renderTraceSpans();
  renderTraceDetail();
  renderTraceList();
}

function orderedTraceSpans(trace) {
  const spans = trace?.spans || [];
  const byParent = new Map();
  spans.forEach(span => {
    const parent = span.parentSpanId || "";
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(span);
  });
  byParent.forEach(children => children.sort((left, right) => (
    Number(BigInt(left.startTimeUnixNano || "0") - BigInt(right.startTimeUnixNano || "0"))
  )));
  const ordered = [];
  const visited = new Set();
  const visit = (span, depth) => {
    if (!span || visited.has(span.spanId)) return;
    visited.add(span.spanId);
    ordered.push({ span, depth });
    (byParent.get(span.spanId) || []).forEach(child => visit(child, depth + 1));
  };
  const root = spans.find(span => span.spanId === trace.rootSpanId)
    || spans.find(span => !span.parentSpanId);
  visit(root, 0);
  spans.forEach(span => visit(span, span.parentSpanId ? 1 : 0));
  return ordered;
}

function renderTraceSpans() {
  const trace = state.activeTrace;
  const ordered = orderedTraceSpans(trace);
  if (!ordered.length) {
    $("traceSpanTree").innerHTML = '<div class="trace-stage-empty"><p>该 OTLP Trace 没有 Span。</p></div>';
    return;
  }
  const root = ordered.find(item => item.span.spanId === trace.rootSpanId)?.span || ordered[0].span;
  const rootStart = BigInt(root.startTimeUnixNano || "0");
  const rootEnd = BigInt(root.endTimeUnixNano || root.startTimeUnixNano || "0");
  const rootDuration = Number(rootEnd > rootStart ? rootEnd - rootStart : 1n);
  $("traceSpanTree").innerHTML = ordered.map(({ span, depth }) => {
    const start = BigInt(span.startTimeUnixNano || "0");
    const end = BigInt(span.endTimeUnixNano || span.startTimeUnixNano || "0");
    const left = Math.max(0, Math.min(100, Number(start - rootStart) / rootDuration * 100));
    const width = Math.max(0, Math.min(100 - left, Number(end - start) / rootDuration * 100));
    return `
      <button class="trace-span-row ${span.spanId === state.activeSpanId ? "active" : ""}"
        data-open-span="${escapeHtml(span.spanId)}" data-kind="${escapeHtml(span.kind)}" data-status="${escapeHtml(span.status)}" type="button">
        <span class="trace-span-name">
          <span class="trace-span-guides">${'<span class="trace-span-guide"></span>'.repeat(depth)}</span>
          <span class="trace-span-status ${escapeHtml(span.status)}"></span>
          <span class="trace-span-name-copy"><strong>${escapeHtml(span.name)}</strong><span>${escapeHtml(span.kind)} · ${escapeHtml(shortId(span.spanId, 16))}</span></span>
        </span>
        <span class="trace-waterfall-track"><span class="trace-waterfall-bar" style="--span-left:${left.toFixed(3)}%;--span-width:${width.toFixed(3)}%"></span></span>
        <span class="trace-span-duration">${escapeHtml(formatDuration(span.durationMs))}</span>
      </button>
    `;
  }).join("");
}

function traceKeyValues(values) {
  const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.length) return '<div class="trace-stage-empty compact"><p>没有可展示的字段。</p></div>';
  return `<dl class="trace-kv-list">${entries.map(([key, value]) => `
    <div class="trace-kv-row"><dt class="trace-kv-key">${escapeHtml(key)}</dt><dd class="trace-kv-value">${escapeHtml(typeof value === "object" ? JSON.stringify(value, null, 2) : value)}</dd></div>
  `).join("")}</dl>`;
}

function setTraceDetailExpanded(expanded) {
  state.traceDetailExpanded = Boolean(expanded);
  $("traceWorkbench").classList.toggle("detail-expanded", state.traceDetailExpanded);
  $("toggleTraceDetail").setAttribute("aria-pressed", String(state.traceDetailExpanded));
  $("toggleTraceDetail").setAttribute(
    "aria-label",
    state.traceDetailExpanded ? "收起 Span 详情" : "展开 Span 详情"
  );
  $("toggleTraceDetail").title = state.traceDetailExpanded ? "收起详情" : "展开详情";
  $("toggleTraceDetail").querySelector("span").textContent = state.traceDetailExpanded ? "收起" : "展开";
}

function renderTraceDetail() {
  const trace = state.activeTrace;
  const span = trace?.spans?.find(item => item.spanId === state.activeSpanId);
  document.querySelectorAll("[data-trace-tab]").forEach(button => {
    const active = button.dataset.traceTab === state.traceTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("traceRawOtlp").hidden = state.traceTab !== "raw";
  $("traceDetailContent").hidden = state.traceTab === "raw";
  $("traceDetail").querySelector(".trace-detail-body").classList.toggle("raw-active", state.traceTab === "raw");
  if (!span) {
    $("traceDetailTitle").textContent = "Span 详情";
    $("traceDetailSubtitle").textContent = "尚未选择 Span";
    $("traceDetailContent").innerHTML = '<div class="trace-stage-empty compact"><p>选择一个 Span 查看标准属性。</p></div>';
    return;
  }
  $("traceDetailTitle").textContent = span.name;
  $("traceDetailSubtitle").textContent = `${span.kind} · ${span.status}`;
  if (state.traceTab === "summary") {
    $("traceDetailContent").innerHTML = `<dl class="trace-detail-grid">
      <div><dt>Trace ID</dt><dd>${escapeHtml(trace.traceId)}</dd></div>
      <div><dt>Span ID</dt><dd>${escapeHtml(span.spanId)}</dd></div>
      <div><dt>Parent</dt><dd>${escapeHtml(span.parentSpanId || "Root")}</dd></div>
      <div><dt>Kind</dt><dd>${escapeHtml(span.kind)}</dd></div>
      <div><dt>Status</dt><dd>${escapeHtml(span.status)}</dd></div>
      <div><dt>开始</dt><dd>${escapeHtml(formatNanoseconds(span.startTimeUnixNano))}</dd></div>
      <div><dt>耗时</dt><dd>${escapeHtml(formatDuration(span.durationMs))}</dd></div>
    </dl>`;
  } else if (state.traceTab === "attributes") {
    $("traceDetailContent").innerHTML = traceKeyValues(span.attributes);
  } else if (state.traceTab === "events") {
    $("traceDetailContent").innerHTML = span.events?.length
      ? `<div class="trace-event-list">${span.events.map(event => `<article class="trace-event-card"><strong>${escapeHtml(event.name)}</strong><span>${escapeHtml(formatNanoseconds(event.timeUnixNano))}</span>${traceKeyValues(event.attributes)}</article>`).join("")}</div>`
      : '<div class="trace-stage-empty compact"><p>该 Span 没有 Events。</p></div>';
  } else if (state.traceTab === "resource") {
    $("traceDetailContent").innerHTML = traceKeyValues({ ...trace.resource, "otel.scope.name": trace.scope?.name, "otel.scope.version": trace.scope?.version });
  } else if (state.traceTab === "raw") {
    loadRawTrace(trace.traceId).catch(handleGlobalError);
  }
}

async function loadRawTrace(traceId) {
  if (state.traceRawOtlp) {
    $("traceRawOtlp").textContent = JSON.stringify(state.traceRawOtlp, null, 2);
    return;
  }
  $("traceRawOtlp").textContent = "正在读取 OTLP JSON…";
  const raw = await api(`/traces/${encodeURIComponent(traceId)}/otlp`);
  if (state.activeTrace?.traceId !== traceId) return;
  state.traceRawOtlp = raw;
  $("traceRawOtlp").textContent = JSON.stringify(raw, null, 2);
}

async function copyRawTrace() {
  const traceId = state.activeTrace?.traceId;
  if (!traceId) return;
  if (!state.traceRawOtlp) await loadRawTrace(traceId);
  if (!state.traceRawOtlp || state.activeTrace?.traceId !== traceId) return;
  await navigator.clipboard.writeText(JSON.stringify(state.traceRawOtlp, null, 2));
  const label = $("copyRawOtlp").querySelector("span");
  label.textContent = "已复制";
  window.setTimeout(() => { label.textContent = "复制 Raw OTLP"; }, 1400);
  showToast("Raw OTLP 已复制", shortId(traceId, 24));
}
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
