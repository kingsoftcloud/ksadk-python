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
