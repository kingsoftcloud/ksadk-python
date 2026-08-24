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
import { OrchestrationPage } from "./pages/OrchestrationPage";
import { EvaluationsPage } from "./pages/EvaluationsPage";
import { EvaluationDetailPage } from "./pages/EvaluationDetailPage";
import { SettingsOverlay, type SettingsSection } from "./components/SettingsOverlay";
import { ChatRunPanel } from "./components/ChatRunPanel";
import { ChatWorkspace } from "./components/ChatWorkspace";
import { CloudChatWorkspace } from "./components/CloudChatWorkspace";
import { AgentAvatar, type AgentAppearance } from "./components/AgentAvatar";
import { ToastRegion } from "./components/Toast";
import { StudioSelect } from "./components/ui/StudioSelect";
import { useStudioViewportMode } from "./useStudioViewportMode";
import { useStudioTheme } from "./useStudioTheme";
import {
  mergeCloudChatTargets,
  resolveCloudChatRoute,
  type AccountCloudAgentSummary,
  type CloudDeploymentSummary,
} from "./cloudDeployments";
import {
  NavigationRail,
  readNavigationRailPreference,
  writeNavigationRailPreference,
  type NavigationView,
} from "./components/NavigationRail";
import { Bot, RefreshCw, PanelLeftClose, PanelLeftOpen, PanelRight } from "lucide-react";

type View = NavigationView;

const VIEW_TITLE: Record<View, string> = {
  agents: "Agent",
  create: "创建 Agent",
  "agent-detail": "Agent 配置",
  conversations: "会话",
  resources: "工程资源",
  builds: "构建",
  deployments: "部署",
  observability: "可观测",
  evaluations: "评测",
  "runtime-resources": "运行资源",
  orchestration: "任务编排",
};

const VALID_VIEWS = Object.keys(VIEW_TITLE) as View[];
const RESOURCE_KINDS: ResourceKind[] = ["model", "tool", "mcp", "skill"];
const AGENT_SCOPED_VIEWS = new Set<View>(["conversations", "builds", "observability", "orchestration"]);

export function parseStudioLocationHash(hash: string): {
  view: View;
  resourceKind: ResourceKind;
  editingAgentId: string;
  detailAgentId: string;
  evaluationRunId: string;
} {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
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
  const view = editingAgentId
    ? "create"
    : detailAgentId
      ? "agent-detail"
      : VALID_VIEWS.includes(candidate)
        ? candidate
        : "agents";
  const resourceKind = view === "resources" && RESOURCE_KINDS.includes(parts[1] as ResourceKind)
    ? parts[1] as ResourceKind
    : "model";
  return { view, resourceKind, editingAgentId, detailAgentId, evaluationRunId };
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
  const viewportMode = useStudioViewportMode();
  const studioTheme = useStudioTheme();
  const initialRoute = parseStudioLocationHash(window.location.hash);
  const [view, setViewState] = useState<View>(initialRoute.view);
  const [evaluationRunId, setEvaluationRunId] = useState(initialRoute.evaluationRunId);
  const [resourceKind, setResourceKind] = useState<ResourceKind>(initialRoute.resourceKind);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [currentAgentId, setCurrentAgentId] = useState(initialRoute.detailAgentId || initialRoute.editingAgentId || "");
  const [detailAgentId, setDetailAgentId] = useState(initialRoute.detailAgentId);
  const [editingAgentId, setEditingAgentId] = useState(initialRoute.editingAgentId);
  const [workspace, setWorkspace] = useState<{ name?: string; path?: string } | null>(null);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [runtimeChecked, setRuntimeChecked] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [chatMounted, setChatMounted] = useState(view === "conversations");
  const [cloudDeployments, setCloudDeployments] = useState<CloudDeploymentSummary[]>([]);
  const [cloudDeploymentId, setCloudDeploymentId] = useState("");
  const [runPanelOpen, setRunPanelOpen] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
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
    const nextHash = v === "resources" ? `#/resources/${resourceKind}` : `#/${v}`;
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
        item.id === previous && resolveCloudChatRoute(item).kind === "studio-session-events"
      )) ? previous : "");
    } catch {
      // Deployment receipts are optional for a local-only workspace.
    }
  }, []);

  useEffect(() => { loadCloudDeployments(); }, [loadCloudDeployments, refreshTick]);

  useEffect(() => {
    apiFetch("/api/v1/system/bootstrap").then(r => r.json()).then(d => {
      setWorkspace(d.workspace || null);
      setRuntimeReady(Boolean(d.workspace));
    }).catch(() => setRuntimeReady(false)).finally(() => setRuntimeChecked(true));
  }, [refreshTick]);

  const currentAgent = agents.find(a => a.metadata.id === currentAgentId);
  const runtimeState = !runtimeChecked ? "pending" : runtimeReady ? "ready" : "failed";
  const runtimeStateLabel = !runtimeChecked ? "检查中" : runtimeReady ? "运行正常" : "连接失败";

  function switchAgent(id: string) {
    if (!id) return;
    setCurrentAgentId(id);
    if (view === "conversations") setChatMounted(true);
  }

  const studioCloudDeployments = cloudDeployments.filter(
    item => resolveCloudChatRoute(item).kind === "studio-session-events",
  );
  const selectedCloudDeployment = studioCloudDeployments.find(item => item.id === cloudDeploymentId);
  const isCloudChat = view === "conversations" && Boolean(selectedCloudDeployment);
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
    const id = agentId || currentAgentId || agents[0]?.metadata.id || "";
    if (!id) { openCreate(); return; }
    setCloudDeploymentId("");
    setCurrentAgentId(id);
    setChatMounted(true);
    setView("conversations");
  }

  function enterCloudChat(deploymentId: string) {
    if (!studioCloudDeployments.some(item => item.id === deploymentId)) return;
    setCloudDeploymentId(deploymentId);
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
  const breadcrumbTitle = view === "create" && editingAgentId ? "编辑 Agent" : VIEW_TITLE[view];

  const workspaceName = workspace?.name || "Workspace";
  const workspacePath = workspace?.path || (runtimeReady ? "本地工作区" : "正在连接本地工作区");
  const focusedView = view === "create"
    || view === "conversations"
    || view === "observability";
  const railCanExpand = viewportMode !== "compact";
  const railExpanded = railCanExpand && (railExpandedPreference ?? true);

  function toggleRail() {
    if (!railCanExpand) return;
    const next = !railExpanded;
    setRailExpandedPreference(next);
    writeNavigationRailPreference(next);
  }

  function navigateFromRail(nextView: NavigationView, kind?: ResourceKind) {
    if (nextView === "conversations") enterChat();
    else if (nextView === "resources") openResources(kind || "model");
    else setView(nextView);
  }

  return (
    <>
      <a className="skip-link" href="#mainContent">跳到主要内容</a>
      <div className="app-shell" data-view={view} data-viewport={viewportMode} data-focused={focusedView} data-rail={railExpanded ? "expanded" : "compact"}>
      <NavigationRail
        view={view}
        resourceKind={resourceKind}
        expanded={railExpanded}
        workspaceName={workspaceName}
        workspacePath={workspacePath}
        runtimeReady={runtimeReady}
        onNavigate={navigateFromRail}
        onOpenSettings={() => {
          setSettingsSection("general");
          setSettingsOpen(true);
        }}
      />

      <div className="app-main">
        <header className={`global-header${breadcrumbParent ? " nested" : ""}`} aria-label="当前页面">
          {railCanExpand && (
            <button
              className="icon-button tertiary rail-toggle"
              type="button"
              aria-label={railExpanded ? "收起导航" : "展开导航"}
              title={railExpanded ? "收起导航" : "展开导航"}
              onClick={toggleRail}
            >
              {railExpanded ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
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
              <strong>{view === "agent-detail" && currentAgent ? currentAgent.metadata.name : breadcrumbTitle}</strong>
              {view === "agent-detail" && currentAgent && (
                <span className="mono">{currentAgent.metadata.id} · r{currentAgent.metadata.revision || 1}</span>
              )}
            </div>
          )}
          {!breadcrumbParent && (
            <div className="header-identity">
              <span>工作区 · {workspaceName}</span>
              <strong>{breadcrumbTitle}</strong>
            </div>
          )}
          <div className="header-actions">
            <div id="pageHeaderTools" className="page-header-tools" data-testid="page-header-tools" />
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
            <button className="icon-button tertiary global-refresh-button" type="button" aria-label="刷新" title="刷新" onClick={() => setRefreshTick(t => t + 1)}>
              <RefreshCw size={16} />
            </button>
            {view === "conversations" && chatMounted && currentAgentId && !isCloudChat && (
              <button className="icon-button tertiary" type="button" aria-label="运行详情" title="运行详情" onClick={() => setRunPanelOpen(v => !v)}>
                <PanelRight size={16} />
              </button>
            )}
            <div id="pageHeaderActions" className="page-header-page-actions" data-testid="page-header-actions" />
          </div>
        </header>

        <main id="mainContent">
          {/* 会话页常驻挂载（display 切换），来回切换不重建工作台 */}
          <div className="chat-wrap" data-layout="workbench" style={{ display: view === "conversations" ? "flex" : "none" }}>
            <div className="chat-host">
              {chatMounted && isCloudChat && selectedCloudDeployment && (
                <CloudChatWorkspace
                  key={selectedCloudDeployment.id}
                  deploymentId={selectedCloudDeployment.id}
                  agentId={selectedCloudDeployment.agentId || "Agent"}
                  agentName={selectedCloudDeployment.agentName || selectedCloudDeployment.agentId || "云端 Agent"}
                  active={view === "conversations"}
                  refreshTick={refreshTick}
                />
              )}
              {chatMounted && !isCloudChat && currentAgentId && (
                <ChatWorkspace
                  key={currentAgentId}
                  agentId={currentAgentId}
                  agentName={currentAgent?.metadata.name || "Agent"}
                  agentAppearance={currentAgent?.metadata.appearance}
                  active={view === "conversations"}
                  refreshTick={refreshTick}
                  onConfigureAgent={() => openEdit(currentAgentId)}
                  onOpenSettings={() => {
                    setSettingsSection("credentials");
                    setSettingsOpen(true);
                  }}
                />
              )}
              {chatMounted && !isCloudChat && !currentAgentId && (
                <div className="empty-state chat-agent-empty" role="status">
                  <span className="empty-icon"><Bot /></span>
                  <h2>{agentsLoaded ? "先创建 Agent 才能开始会话" : "正在载入 Agent"}</h2>
                  <p>{agentsLoaded ? "会话会使用当前 Agent 的模型、工具与运行时配置。" : "正在同步本地工作区…"}</p>
                  {agentsLoaded && <button className="primary-button" type="button" onClick={openCreate}>创建 Agent</button>}
                </div>
              )}
            </div>
            {runPanelOpen && chatMounted && currentAgentId && !isCloudChat && (
              <ChatRunPanel agentId={currentAgentId} onClose={() => setRunPanelOpen(false)} onOpenTrace={() => setView("observability")} />
            )}
          </div>

          <div style={{ display: view === "conversations" ? "none" : undefined }}>
            {view === "agents" && (
              <AgentsPage
                agents={agents}
                runtimeReady={runtimeReady}
                runtimeChecked={runtimeChecked}
                workspaceName={workspace?.name || ""}
                onCreate={openCreate}
                onDetail={openDetail}
                onChat={enterChat}
                onBuild={() => setView("builds")}
                onChanged={loadAgents}
              />
            )}
            {view === "create" && (
              <CreatePage
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
                agentId={detailAgentId}
                onBack={() => setView("agents")}
                onChat={enterChat}
                onBuild={() => setView("builds")}
                onEdit={openEdit}
                onChanged={loadAgents}
              />
            )}
            {view === "resources" && <ResourcesPage kind={resourceKind} onKindChange={openResources} refreshTick={refreshTick} />}
            {view === "builds" && <BuildsPage currentAgentId={currentAgentId} agents={agents} onSelectAgent={setCurrentAgentId} onCreate={openCreate} />}
            {view === "deployments" && (
              <DeploymentsPage
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
              <EvaluationDetailPage runId={evaluationRunId} onBack={closeEvaluationRun} />
            )}
            {view === "runtime-resources" && <RuntimeResourcesPage refreshTick={refreshTick} onOpenResources={openResources} />}
            {view === "orchestration" && <OrchestrationPage currentAgentId={currentAgentId} agents={agents} onSelectAgent={setCurrentAgentId} onCreate={openCreate} />}
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
