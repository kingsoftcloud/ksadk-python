import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";
import { AgentsPage } from "./pages/AgentsPage";
import { CreatePage } from "./pages/CreatePage";
import { AgentDetailPage } from "./pages/AgentDetailPage";
import { BuildsPage } from "./pages/BuildsPage";
import { DeploymentsPage } from "./pages/DeploymentsPage";
import { ResourcesPage, type ResourceKind } from "./pages/ResourcesPage";
import { ObservabilityPage } from "./pages/ObservabilityPage";
import { RuntimeResourcesPage } from "./pages/RuntimeResourcesPage";
import { PluginsPage } from "./pages/PluginsPage";
import { PluginWorkspacePage } from "./plugins/PluginWorkspacePage";
import { useWorkspaceContributions } from "./plugins/workspaceSlots";
import { AutomationsPage } from "./pages/AutomationsPage";
import { EvaluationsPage } from "./pages/EvaluationsPage";
import { EvaluationDetailPage } from "./pages/EvaluationDetailPage";
import { SettingsOverlay, type SettingsSection } from "./components/SettingsOverlay";
import { MoreActionsMenu } from "./components/MoreActionsMenu";
import { ChatRunPanel } from "./components/ChatRunPanel";
import { ChatWorkspace } from "./components/ChatWorkspace";
import { AgentAvatar, type AgentAppearance } from "./components/AgentAvatar";
import { ToastRegion, showToast } from "./components/Toast";
import { StudioSelect } from "./components/ui/StudioSelect";
import { useStudioViewportMode } from "./useStudioViewportMode";
import { useStudioTheme } from "./useStudioTheme";
import {
  mergeCloudChatTargets,
  resolveCloudChatRoute,
  isCloudChatTargetSelectable,
  type AccountCloudAgentSummary,
  type CloudDeploymentSummary,
} from "./cloudDeployments";
import {
  NavigationRail,
  readNavigationRailPreference,
  writeNavigationRailPreference,
  type NavigationView,
} from "./components/NavigationRail";
import { PanelRight } from "lucide-react";
import { KingIcon } from "./components/KingIcon";

type View = NavigationView;

const VIEW_TITLE: Record<View, string> = {
  agents: "Agent",
  create: "创建 Agent",
  "agent-detail": "Agent 配置",
  conversations: "会话",
  resources: "资源库",
  builds: "构建",
  deployments: "部署",
  observability: "可观测",
  evaluations: "评测",
  "runtime-resources": "运行资源",
  plugins: "已安装插件",
  automations: "自动化",
};

const VALID_VIEWS = Object.keys(VIEW_TITLE) as View[];
const RESOURCE_KINDS: ResourceKind[] = [
  "model", "tool", "mcp", "skill", "knowledge-base", "memory-instance", "skill-space",
];
const AGENT_SCOPED_VIEWS = new Set<View>(["conversations", "builds"]);
const CHAT_TARGET_STORAGE_KEY = "agentkit-studio:chat-target:v1";

function storedChatTarget(): ReturnType<typeof parseChatTargetValue> {
  try {
    return parseChatTargetValue(window.localStorage.getItem(CHAT_TARGET_STORAGE_KEY) || "");
  } catch {
    return { kind: "", id: "" };
  }
}

function rememberChatTarget(value: string): void {
  try {
    window.localStorage.setItem(CHAT_TARGET_STORAGE_KEY, value);
  } catch {
    // Studio remains usable when storage is disabled by the embedding shell.
  }
}

export function parseStudioLocationHash(hash: string): {
  view: View;
  resourceKind: ResourceKind;
  editingAgentId: string;
  detailAgentId: string;
  evaluationRunId: string;
  conversationAgentId?: string;
  sessionId?: string;
} {
  const [pathname, search = ""] = hash.replace(/^#\/?/, "").split("?");
  const parts = pathname.split("/").filter(Boolean);
  const editingAgentId = parts[0] === "agents" && parts[1] && parts[2] === "edit"
    ? decodeURIComponent(parts[1])
    : "";
  const detailAgentId = parts[0] === "agents" && parts[1] && !parts[2]
    ? decodeURIComponent(parts[1])
    : "";
  const evaluationRunId = parts[0] === "evaluations" && parts[1]
    ? decodeURIComponent(parts[1])
    : "";
  const candidate = parts[0] as View;
  const pluginPageId = parts[0] === 'workspace' && parts[1] ? decodeURIComponent(parts[1]) : ['orchestration', 'teams'].includes(parts[0]) ? 'teams' : '';
  const view: View = pluginPageId ? `plugin:${pluginPageId}` : editingAgentId
    ? "create"
    : detailAgentId
      ? "agent-detail"
      : VALID_VIEWS.includes(candidate)
        ? candidate
        : "agents";
  const resourceKind = view === "resources" && RESOURCE_KINDS.includes(parts[1] as ResourceKind)
    ? parts[1] as ResourceKind
    : "model";
  const params = new URLSearchParams(search);
  return { view, resourceKind, editingAgentId, detailAgentId, evaluationRunId, ...(view === "conversations" && params.has("agentId") ? { conversationAgentId: params.get("agentId") || "", sessionId: params.get("sessionId") || "" } : {}) };
}

export function parseChatTargetValue(value: string): {
  kind: "cloud" | "local" | "";
  id: string;
} {
  const separator = value.indexOf(":");
  if (separator <= 0) return { kind: "", id: "" };
  const kind = value.slice(0, separator);
  if (kind !== "cloud" && kind !== "local") return { kind: "", id: "" };
  return { kind, id: value.slice(separator + 1) };
}

interface AgentSummary {
  metadata: { id: string; name: string; revision?: number; labels?: Record<string, string>; appearance?: AgentAppearance };
  spec?: { runtime?: { type?: string } };
  builds?: Array<{ id: string; status: string }>;
}

export default function App() {
  const workspacePages = useWorkspaceContributions();
  const viewportMode = useStudioViewportMode();
  const studioTheme = useStudioTheme();
  const initialRoute = parseStudioLocationHash(window.location.hash);
  const [initialChatTarget] = useState(storedChatTarget);
  const [view, setViewState] = useState<View>(initialRoute.view);
  const [evaluationRunId, setEvaluationRunId] = useState(initialRoute.evaluationRunId);
  const [resourceKind, setResourceKind] = useState<ResourceKind>(initialRoute.resourceKind);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [currentAgentId, setCurrentAgentId] = useState(
    initialRoute.detailAgentId
      || initialRoute.editingAgentId
      || initialRoute.conversationAgentId
      || (initialRoute.view === "conversations" && initialChatTarget.kind === "local"
        ? initialChatTarget.id
        : ""),
  );
  const [automationAgentScopeId, setAutomationAgentScopeId] = useState("");
  const [requestedSessionId, setRequestedSessionId] = useState(initialRoute.sessionId || "");
  const [detailAgentId, setDetailAgentId] = useState(initialRoute.detailAgentId);
  const [editingAgentId, setEditingAgentId] = useState(initialRoute.editingAgentId);
  const [workspace, setWorkspace] = useState<{ name?: string; path?: string; workspaceId?: string } | null>(null);
  const [workspaces, setWorkspaces] = useState<Array<{ workspaceId: string; name: string; path: string }>>([]);
  const [workspaceRunCount, setWorkspaceRunCount] = useState(0);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [runtimeChecked, setRuntimeChecked] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [chatMounted, setChatMounted] = useState(view === "conversations");
  const [cloudDeployments, setCloudDeployments] = useState<CloudDeploymentSummary[]>([]);
  const [cloudDeploymentsLoaded, setCloudDeploymentsLoaded] = useState(false);
  const [cloudDeploymentId, setCloudDeploymentId] = useState(
    !initialRoute.conversationAgentId && initialChatTarget.kind === "cloud" ? initialChatTarget.id : "",
  );
  const [runPanelOpen, setRunPanelOpen] = useState(false);
  const [conversationSessionId, setConversationSessionId] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [newChatRequest, setNewChatRequest] = useState(0);
  const onNewChatStarted = useCallback(() => setNewChatRequest(0), []);
  const [conversationHeaderHost, setConversationHeaderHost] = useState<HTMLDivElement | null>(null);
  const [chatStreaming, setChatStreaming] = useState(false);
  const [historyHost, setHistoryHost] = useState<HTMLDivElement | null>(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [railExpandedPreference, setRailExpandedPreference] = useState<boolean | null>(readNavigationRailPreference);
  useEffect(() => {
    document.body.classList.toggle("create-mode", view === "create");
    return () => document.body.classList.remove("create-mode");
  }, [view]);

  useEffect(() => {
    const syncViewFromHash = () => {
      const route = parseStudioLocationHash(window.location.hash);
      setViewState(route.view);
      setResourceKind(route.resourceKind);
      setEditingAgentId(route.editingAgentId);
      setDetailAgentId(route.detailAgentId);
      setEvaluationRunId(route.evaluationRunId);
      if (route.conversationAgentId) {
        setCurrentAgentId(route.conversationAgentId);
        setCloudDeploymentId("");
      }
      setRequestedSessionId(route.sessionId || "");
      if (route.editingAgentId || route.detailAgentId) {
        setCurrentAgentId(route.editingAgentId || route.detailAgentId);
      }
      if (route.view === "conversations") setChatMounted(true);
    };
    window.addEventListener("hashchange", syncViewFromHash);
    window.addEventListener("popstate", syncViewFromHash);
    return () => {
      window.removeEventListener("hashchange", syncViewFromHash);
      window.removeEventListener("popstate", syncViewFromHash);
    };
  }, []);

  // hash 深链：#/agents 等，便于刷新定位
  function setView(v: View) {
    setViewState(v);
    if (v === "conversations") setChatMounted(true);
    if (v !== "create") setEditingAgentId("");
    setEvaluationRunId("");
    const nextHash = v === "resources" ? `#/resources/${resourceKind}` : v.startsWith("plugin:") ? `#/workspace/${encodeURIComponent(v.slice(7))}` : `#/${v}`;
    if (window.location.hash !== nextHash) window.history.pushState(null, "", nextHash);
  }

  function openEvaluationRun(runId: string) {
    setViewState("evaluations");
    setEvaluationRunId(runId);
    window.history.pushState(null, "", `#/evaluations/${encodeURIComponent(runId)}`);
  }

  function closeEvaluationRun() {
    setEvaluationRunId("");
    window.history.pushState(null, "", "#/evaluations");
  }

  const loadAgents = useCallback(async () => {
    try {
      const payload = await apiFetch("/api/v1/agents?limit=100").then(r => r.json());
      const summaries: AgentSummary[] = payload.items || [];
      const details = await Promise.all(summaries.map(agent => (
        apiFetch(`/api/v1/agents/${encodeURIComponent(agent.metadata.id)}`)
          .then(r => r.ok ? r.json() : null)
          .catch(() => null)
      )));
      const items = summaries.map((agent, index) => ({
        ...agent,
        builds: details[index]?.builds || [],
      }));
      setAgents(items);
      setCurrentAgentId(prev => (
        items.some(agent => agent.metadata.id === prev)
          ? prev
          : items[0]?.metadata.id || ""
      ));
    } catch {
      // 保留上一次成功加载的数据，刷新按钮可重新触发同步。
    } finally {
      setAgentsLoaded(true);
    }
  }, []);

  useEffect(() => { loadAgents(); }, [loadAgents, refreshTick]);

  const loadCloudDeployments = useCallback(async () => {
    try {
      const [receiptResponse, accountResponse] = await Promise.all([
        apiFetch("/api/v1/deployments"),
        apiFetch("/api/v1/cloud-agents?size=100"),
      ]);
      if (!receiptResponse.ok) return;
      const receiptPayload = await receiptResponse.json() as { items?: CloudDeploymentSummary[] };
      const accountPayload = accountResponse.ok
        ? await accountResponse.json() as { items?: AccountCloudAgentSummary[] }
        : { items: [] };
      const receiptItems = receiptPayload.items || [];
      const accountItems = accountPayload.items || [];
      const receiptAgentIds = [...new Set(receiptItems.flatMap((item: CloudDeploymentSummary) => (
        item.agentId?.trim() ? [item.agentId.trim()] : []
      )))];
      const accountDetails = await Promise.all<AccountCloudAgentSummary | null>(receiptAgentIds.map(agentId => (
        Promise.resolve()
          .then(() => apiFetch(`/api/v1/cloud-agents/${encodeURIComponent(agentId)}`))
          .then(async response => response.ok
            ? await response.json() as AccountCloudAgentSummary
            : null)
          .catch(() => null)
      )));
      const accountByAgentId = new Map(accountItems.map(item => [item.agentId, item]));
      for (const detail of accountDetails) {
        if (detail?.agentId) accountByAgentId.set(detail.agentId, { ...accountByAgentId.get(detail.agentId), ...detail });
      }
      const items = mergeCloudChatTargets(
        receiptItems,
        [...accountByAgentId.values()],
      );
      setCloudDeployments(items);
      setCloudDeploymentId(previous => items.some((item: CloudDeploymentSummary) => (
        item.id === previous
        && resolveCloudChatRoute(item).kind === "studio-session-events"
        && isCloudChatTargetSelectable(item)
      )) ? previous : "");
    } catch {
      // Deployment receipts are optional for a local-only workspace.
    } finally {
      setCloudDeploymentsLoaded(true);
    }
  }, []);

  useEffect(() => { loadCloudDeployments(); }, [loadCloudDeployments, refreshTick]);

  useEffect(() => {
    apiFetch("/api/v1/system/bootstrap").then(r => r.json()).then(d => {
      setWorkspace(d.workspace || null);
      setRuntimeReady(Boolean(d.workspace));
    }).catch(() => setRuntimeReady(false)).finally(() => setRuntimeChecked(true));
  }, [refreshTick]);

  useEffect(() => {
    apiFetch("/api/v1/workspaces/runs").then(r => r.ok ? r.json() : null)
      .then(d => setWorkspaceRunCount(Array.isArray(d?.items) ? d.items.filter((item: any) => ["running", "pending", "input-required", "paused"].includes(item.status)).length : 0))
      .catch(() => undefined);
  }, [refreshTick]);

  useEffect(() => {
    apiFetch("/api/v1/workspaces").then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.items) setWorkspaces(d.items); }).catch(() => undefined);
  }, [refreshTick]);

  const currentAgent = agents.find(a => a.metadata.id === currentAgentId);
  const runtimeState = !runtimeChecked ? "pending" : runtimeReady ? "ready" : "failed";
  const runtimeStateLabel = !runtimeChecked ? "检查中" : runtimeReady ? "运行正常" : "连接失败";

  function switchAgent(id: string) {
    if (!id) return;
    setCurrentAgentId(id);
    if (view === "automations" && automationAgentScopeId) setAutomationAgentScopeId(id);
    if (view === "conversations") setChatMounted(true);
  }

  const studioCloudDeployments = cloudDeployments.filter(
    item => resolveCloudChatRoute(item).kind === "studio-session-events"
      && isCloudChatTargetSelectable(item),
  );
  const selectedCloudDeployment = studioCloudDeployments.find(item => item.id === cloudDeploymentId);
  const isCloudChat = view === "conversations" && Boolean(selectedCloudDeployment);

  useEffect(() => {
    if (view !== "conversations" || !agentsLoaded || !cloudDeploymentsLoaded) return;
    if (selectedCloudDeployment) {
      rememberChatTarget(`cloud:${selectedCloudDeployment.id}`);
    } else if (currentAgent) {
      rememberChatTarget(`local:${currentAgent.metadata.id}`);
    }
  }, [
    agentsLoaded,
    cloudDeploymentsLoaded,
    currentAgent,
    selectedCloudDeployment,
    view,
  ]);

  useEffect(() => {
    if (
      view !== "conversations"
      || !chatMounted
      || !agentsLoaded
      || !cloudDeploymentsLoaded
      // A newly created local Agent is selected before the asynchronous
      // directory refresh has returned it.  Do not mistake that short window
      // for "no local target" and replace the explicit selection with the
      // remembered cloud target.
      || Boolean(currentAgentId)
      || currentAgent
      || selectedCloudDeployment
    ) return;
    const fallback = studioCloudDeployments[0];
    if (!fallback) return;
    setCloudDeploymentId(fallback.id);
    setRunPanelOpen(false);
  }, [
    agentsLoaded,
    chatMounted,
    cloudDeploymentsLoaded,
    currentAgentId,
    currentAgent,
    selectedCloudDeployment,
    studioCloudDeployments,
    view,
  ]);

  const chatTargetOptions = [
    ...agents.map(agent => ({ value: `local:${agent.metadata.id}`, label: `本地 · ${agent.metadata.name}` })),
    ...studioCloudDeployments.map(deployment => ({
      value: `cloud:${deployment.id}`,
      label: `云端 · ${deployment.agentName || deployment.agentId}`,
    })),
  ];
  const chatTargetValue = selectedCloudDeployment
    ? `cloud:${selectedCloudDeployment.id}`
    : currentAgentId
      ? `local:${currentAgentId}`
      : "";

  function switchChatTarget(value: string) {
    const { kind, id } = parseChatTargetValue(value);
    if (kind === "cloud" && id) {
      if (!studioCloudDeployments.some(item => item.id === id)) return;
      setCloudDeploymentId(id);
      setRunPanelOpen(false);
      setChatMounted(true);
      return;
    }
    if (kind === "local" && id) {
      setCloudDeploymentId("");
      switchAgent(id);
    }
  }

  function enterChat(agentId?: string) {
    const localTarget = currentAgent || agents[0];
    const cloudTarget = selectedCloudDeployment || studioCloudDeployments[0];
    if (agentId) {
      setCloudDeploymentId("");
      setCurrentAgentId(agentId);
    } else if (!agentId && selectedCloudDeployment) {
      setCurrentAgentId("");
    } else if (localTarget) {
      setCloudDeploymentId("");
      setCurrentAgentId(localTarget.metadata.id);
    } else if (cloudTarget) {
      setCurrentAgentId("");
      setCloudDeploymentId(cloudTarget.id);
      setRunPanelOpen(false);
    } else {
      setCurrentAgentId("");
      setCloudDeploymentId("");
    }
    setChatMounted(true);
    setView("conversations");
  }

  function enterCloudChat(target: CloudDeploymentSummary) {
    if (resolveCloudChatRoute(target).kind !== "studio-session-events") return;
    // DeploymentsPage already owns a live target projection.  Do not discard a
    // click merely because this App-level directory is still in flight: with a
    // large cloud directory that race used to leave the user in the local
    // empty-state, which looks like being redirected to Create Agent.
    setCloudDeployments(current => (
      current.some(item => item.id === target.id)
        ? current
        : [...current, target]
    ));
    setCloudDeploymentId(target.id);
    setRunPanelOpen(false);
    setChatMounted(true);
    setView("conversations");
  }

  function openDetail(agentId: string) {
    setEditingAgentId("");
    setDetailAgentId(agentId);
    setCurrentAgentId(agentId);
    setViewState("agent-detail");
    const nextHash = `#/agents/${encodeURIComponent(agentId)}`;
    if (window.location.hash !== nextHash) window.history.pushState(null, "", nextHash);
  }

  function openCreate() {
    setEditingAgentId("");
    setView("create");
  }

  function openEdit(agentId: string) {
    setEditingAgentId(agentId);
    setCurrentAgentId(agentId);
    setViewState("create");
    const nextHash = `#/agents/${encodeURIComponent(agentId)}/edit`;
    if (window.location.hash !== nextHash) window.history.pushState(null, "", nextHash);
  }

  function openResources(kind: ResourceKind) {
    setResourceKind(kind);
    setViewState("resources");
    const nextHash = `#/resources/${kind}`;
    if (window.location.hash !== nextHash) window.history.pushState(null, "", nextHash);
  }

  const breadcrumbParent = view === "create" || view === "agent-detail" ? "Agent" : null;
  const pluginPageId = view.startsWith("plugin:") ? view.slice(7) : "";
  const breadcrumbTitle = view === "create" && editingAgentId ? "编辑 Agent" : pluginPageId ? (workspacePages.find(page => page.id === pluginPageId)?.label || (pluginPageId === "teams" ? "团队" : "插件工作区")) : VIEW_TITLE[view];

  const workspaceName = workspace?.path?.endsWith("/default-workspace") ? "未打开工作区" : (workspace?.name || "未打开工作区");
  const workspacePath = workspace?.path || (runtimeReady ? "本地工作区" : "正在连接本地工作区");
  const focusedView = view === "create"
    || view === "conversations"
    || view === "observability"
    || Boolean(pluginPageId);
  const railCanExpand = viewportMode !== "compact";
  const railExpanded = railCanExpand && (railExpandedPreference ?? true);
  useEffect(() => { setMobileNavOpen(false); }, [view, resourceKind, viewportMode]);

  function toggleRail() {
    if (!railCanExpand) { setMobileNavOpen(open => !open); return; }
    const next = !railExpanded;
    setRailExpandedPreference(next);
    writeNavigationRailPreference(next);
  }

  function navigateFromRail(nextView: NavigationView, kind?: ResourceKind) {
    setMobileNavOpen(false);
    if (nextView === "conversations") enterChat();
    else if (nextView === "resources") openResources(kind || "model");
    else {
      if (nextView === "automations") setAutomationAgentScopeId("");
      setView(nextView);
    }
  }

  return (
    <>
      <a className="skip-link" href="#mainContent">跳到主要内容</a>
      <div className="app-shell" data-view={view} data-viewport={viewportMode} data-focused={focusedView} data-rail={railExpanded ? "expanded" : "compact"}>
      <NavigationRail
        workspacePages={workspacePages}
        view={view}
        resourceKind={resourceKind}
        expanded={railExpanded}
        mobile={!railCanExpand}
        mobileOpen={mobileNavOpen}
        onMobileOpenChange={setMobileNavOpen}
        onExpand={() => { setRailExpandedPreference(true); writeNavigationRailPreference(true); }}
        onHistoryHostChange={setHistoryHost}
        chatStreaming={chatStreaming || newChatRequest !== 0}
        onStartChat={() => { setRequestedSessionId(""); setNewChatRequest(request => request + 1); enterChat(); setMobileNavOpen(false); }}
        workspaceName={workspaceName}
        workspacePath={workspacePath}
        runtimeReady={runtimeReady}
        workspaceRunCount={workspaceRunCount}
        onNavigate={navigateFromRail}
        onOpenSettings={() => {
          setMobileNavOpen(false);
          setSettingsSection("general");
          setSettingsOpen(true);
        }}
        onWorkspaceSwitch={async () => {
          setMobileNavOpen(false);
          try {
            if (window.studioNative?.openWorkspace) {
              await window.studioNative.openWorkspace();
              return;
            }
            let path = await window.studioNative?.chooseWorkspace?.();
            if (path === undefined) {
              const picked = await apiFetch("/api/v1/workspaces:choose", { method: "POST" });
              const payload = await picked.json() as { path?: string | null; error?: { message?: string } };
              if (!picked.ok) throw new Error(payload.error?.message || "当前环境无法打开目录选择器");
              path = payload.path || null;
            }
            if (!path) return;
            const opened = await apiFetch("/api/v1/workspaces:open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, create: true }) });
            if (!opened.ok) {
              const payload = await opened.json().catch(() => ({})) as { error?: { message?: string }; detail?: string };
              throw new Error(payload.error?.message || payload.detail || `打开工作区失败（${opened.status}）`);
            }
            window.dispatchEvent(new Event("studio:directory-opened"));
          } catch (error) {
            showToast("工作区切换失败", error instanceof Error ? error.message : "无法打开所选目录。", "error");
          }
        }}
      />

      <div className="app-main">
        <header className={`global-header${breadcrumbParent ? " nested" : ""}`} aria-label="当前页面">
          {(
            <button
              className="icon-button tertiary rail-toggle"
              type="button"
              aria-label={(railExpanded || mobileNavOpen) ? "收起导航" : "展开导航"}
              aria-expanded={railExpanded || mobileNavOpen}
              title={railExpanded ? "收起导航" : "展开导航"}
              onClick={toggleRail}
            >
              <KingIcon name={railExpanded ? "left-squared" : "right-squared"} size={16} />
            </button>
          )}
          {breadcrumbParent && (
            <div className="header-identity-inline" aria-label="当前位置">
              <button className="crumb" type="button" onClick={() => setView("agents")}>{breadcrumbParent}</button>
              <span className="crumb-sep">/</span>
              {view === "agent-detail" && currentAgent && (
                <AgentAvatar
                  name={currentAgent.metadata.name}
                  appearance={currentAgent.metadata.appearance}
                  template={currentAgent.metadata.labels?.["agentkit.ksyun.com/template"]}
                  size="sm"
                />
              )}
              <h1>{view === "agent-detail" && currentAgent ? currentAgent.metadata.name : breadcrumbTitle}</h1>
              {view === "agent-detail" && currentAgent && (
                <span className="mono">{currentAgent.metadata.id} · r{currentAgent.metadata.revision || 1}</span>
              )}
            </div>
          )}
          {!breadcrumbParent && view !== "conversations" && (
            <div className="header-identity">
              <h1>{breadcrumbTitle}</h1>
            </div>
          )}
          <div className="header-actions">
            <div ref={setConversationHeaderHost} id="pageHeaderTools" className="page-header-tools" data-testid="page-header-tools" />
            {view === "conversations" ? (
              <StudioSelect
                className="header-agent-selector conversation-target-selector"
                ariaLabel="切换会话目标"
                value={chatTargetValue}
                placeholder="选择会话目标"
                options={chatTargetOptions}
                onValueChange={switchChatTarget}
              />
            ) : AGENT_SCOPED_VIEWS.has(view) && (
              <StudioSelect
                className="header-agent-selector"
                ariaLabel="切换当前 Agent"
                value={currentAgentId}
                placeholder="未选择"
                options={agents.map(agent => ({ value: agent.metadata.id, label: agent.metadata.name }))}
                onValueChange={switchAgent}
              />
            )}
            {view !== "conversations" && <span className="tag">{isCloudChat ? "云端部署" : "本地"}</span>}
            <span className="badge" data-state={runtimeState}>{runtimeStateLabel}</span>
            {(view !== "conversations" || railCanExpand) && <button className="icon-button tertiary global-refresh-button" type="button" aria-label="刷新" title="刷新" onClick={() => setRefreshTick(t => t + 1)}>
              <KingIcon name="refresh" size={16} />
            </button>}
            {view === "conversations" && railCanExpand && chatMounted && currentAgentId && !isCloudChat && (
              <button className="icon-button tertiary conversation-run-detail" type="button" aria-label="运行详情" title="运行详情" onClick={() => setRunPanelOpen(v => !v)}>
                <PanelRight size={16} />
              </button>
            )}
            {view === "conversations" && !railCanExpand && <MoreActionsMenu label="对话操作" items={[
              { label: "刷新", onSelect: () => setRefreshTick(t => t + 1) },
              ...(chatMounted && currentAgentId && !isCloudChat ? [{ label: "运行详情", onSelect: () => setRunPanelOpen(v => !v) }] : []),
            ]}/>}
            <div id="pageHeaderActions" className="page-header-page-actions" data-testid="page-header-actions" />
          </div>
        </header>

        <main id="mainContent">
          {/* 会话页常驻挂载（display 切换），来回切换不重建工作台 */}
          <div className="chat-wrap" data-layout="workbench" style={{ display: view === "conversations" ? "flex" : "none" }}>
            <div className="chat-host">
              {chatMounted && isCloudChat && selectedCloudDeployment && (
                <ChatWorkspace
                  newChatRequest={newChatRequest}
                  onNewChatStarted={onNewChatStarted}
                  onStreamingChange={setChatStreaming}
                  integratedHistory
                  historyHost={historyHost}
                  headerHost={conversationHeaderHost}
                  onSelectConversation={() => { enterChat(); setMobileNavOpen(false); }}
                  key={selectedCloudDeployment.id}
                  agentId={selectedCloudDeployment.agentId || "Agent"}
                  agentName={selectedCloudDeployment.agentName || selectedCloudDeployment.agentId || "云端 Agent"}
                  active={view === "conversations"}
                  refreshTick={refreshTick}
                />
              )}
              {chatMounted && !isCloudChat && currentAgentId && (
                <ChatWorkspace
                  newChatRequest={newChatRequest}
                  onNewChatStarted={onNewChatStarted}
                  onStreamingChange={setChatStreaming}
                  integratedHistory
                  historyHost={historyHost}
                  headerHost={conversationHeaderHost}
                  onSelectConversation={() => { enterChat(); setMobileNavOpen(false); }}
                  key={currentAgentId}
                  agentId={currentAgentId}
                  requestedSessionId={requestedSessionId}
                  agentName={currentAgent?.metadata.name || "Agent"}
                  agentAppearance={currentAgent?.metadata.appearance}
                  active={view === "conversations"}
                  refreshTick={refreshTick}
                  onSessionChanged={setConversationSessionId}
                  onConfigureAgent={() => openEdit(currentAgentId)}
                  onOpenSettings={() => {
                    setSettingsSection("credentials");
                    setSettingsOpen(true);
                  }}
                />
              )}
              {chatMounted && !isCloudChat && !currentAgentId && (
                agentsLoaded && cloudDeploymentsLoaded ? (
                  <div className="empty-state chat-agent-empty" role="status">
                    <span className="empty-icon"><KingIcon name="cpu" size={24} /></span>
                    <h2>还没有可用的会话目标</h2>
                    <p>可以创建本地 Agent，或在云端 Agent 页面选择受支持的 Agent。</p>
                    <div className="empty-actions">
                      <button className="primary-button" type="button" onClick={openCreate}>创建本地 Agent</button>
                      <button className="button secondary" type="button" onClick={() => setView("deployments")}>查看云端 Agent</button>
                    </div>
                  </div>
                ) : (
                  <div className="chat-target-loading" role="status" aria-label="正在同步会话目标">
                    <i />
                    <span>正在同步会话目标…</span>
                  </div>
                )
              )}
            </div>
            {runPanelOpen && chatMounted && currentAgentId && !isCloudChat && (
              <ChatRunPanel agentId={currentAgentId} sessionId={conversationSessionId} onClose={() => setRunPanelOpen(false)} onOpenTrace={() => setView("observability")} />
            )}
          </div>

          {pluginPageId && <div className="studio-plugin-page-host"><PluginWorkspacePage pageId={pluginPageId} contributions={workspacePages} /></div>}
          <div style={{ display: view === "conversations" || pluginPageId ? "none" : undefined }}>
            {view === "agents" && (
              <AgentsPage
                agents={agents}
                runtimeReady={runtimeReady}
                runtimeChecked={runtimeChecked}
                workspaceName={workspace?.name || ""}
                onCreate={openCreate}
                onDetail={openDetail}
                onChat={enterChat}
                onBuild={id => { setCurrentAgentId(id); setView("builds"); }}
                onChanged={loadAgents}
              />
            )}
            {view === "create" && (
              <CreatePage
                workspacePath={workspace?.path}
                editingAgentId={editingAgentId || undefined}
                viewportMode={viewportMode}
                onAgentsChanged={loadAgents}
                onBack={() => editingAgentId ? openDetail(editingAgentId) : setView("agents")}
                onCreated={(id, openChat) => {
                  setEditingAgentId("");
                  loadAgents();
                  if (id && openChat) enterChat(id);
                  else if (id) openDetail(id);
                  else setView("agents");
                }}
              />
            )}
            {view === "agent-detail" && detailAgentId && (
              <AgentDetailPage
                refreshTick={refreshTick}
                agentId={detailAgentId}
                onBack={() => setView("agents")}
                onChat={enterChat}
                onBuild={() => { setCurrentAgentId(detailAgentId); setView("builds"); }}
                onEdit={openEdit}
                onChanged={loadAgents}
              />
            )}
            {view === "resources" && <ResourcesPage kind={resourceKind} onKindChange={openResources} refreshTick={refreshTick} />}
            {view === "builds" && <BuildsPage currentAgentId={currentAgentId} agents={agents} onSelectAgent={setCurrentAgentId} onCreate={openCreate} refreshTick={refreshTick} />}
            {view === "deployments" && (
              <DeploymentsPage
                refreshTick={refreshTick}
                onCreate={openCreate}
                onOpenChat={enterCloudChat}
                onSelectBuild={() => setView("builds")}
              />
            )}
            {view === "observability" && (
              <ObservabilityPage refreshTick={refreshTick} />
            )}
            {view === "evaluations" && !evaluationRunId && (
              <EvaluationsPage refreshTick={refreshTick} onOpenRun={openEvaluationRun} />
            )}
            {view === "evaluations" && evaluationRunId && (
              <EvaluationDetailPage runId={evaluationRunId} onBack={closeEvaluationRun} refreshTick={refreshTick} />
            )}
            {view === "runtime-resources" && <RuntimeResourcesPage refreshTick={refreshTick} onOpenResources={openResources} />}
            {view === "plugins" && <PluginsPage refreshTick={refreshTick} />}
            {view === "automations" && <AutomationsPage currentAgentId={currentAgentId} agents={agents} onSelectAgent={setCurrentAgentId} scopedAgentId={automationAgentScopeId} refreshTick={refreshTick} />}
          </div>
        </main>
      </div>

        {settingsOpen && (
          <SettingsOverlay
            themePreference={studioTheme.preference}
            onThemePreferenceChange={studioTheme.setPreference}
            initialSection={settingsSection}
            onClose={() => setSettingsOpen(false)}
          />
        )}
        <ToastRegion />
      </div>
    </>
  );
}
