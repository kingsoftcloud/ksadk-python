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
import { SettingsOverlay } from "./components/SettingsOverlay";
import { ChatRunPanel } from "./components/ChatRunPanel";
import { ChatWorkspace } from "./components/ChatWorkspace";
import type { AgentAppearance } from "./components/AgentAvatar";
import { ToastRegion } from "./components/Toast";
import { StudioSelect } from "./components/ui/StudioSelect";
import { useStudioViewportMode } from "./useStudioViewportMode";
import { useStudioTheme } from "./useStudioTheme";
import {
  NavigationRail,
  readNavigationRailPreference,
  writeNavigationRailPreference,
  type NavigationView,
} from "./components/NavigationRail";
import {
  Bot, ChevronDown, RefreshCw, PanelLeftClose, PanelLeftOpen, PanelRight,
} from "lucide-react";

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
  "runtime-resources": "运行资源",
  orchestration: "任务编排",
};

const VALID_VIEWS = Object.keys(VIEW_TITLE) as View[];

interface AgentSummary {
  metadata: { id: string; name: string; revision?: number; labels?: Record<string, string>; appearance?: AgentAppearance };
  spec?: { runtime?: { type?: string } };
  builds?: Array<{ id: string; status: string }>;
}

export default function App() {
  const viewportMode = useStudioViewportMode();
  const studioTheme = useStudioTheme();
  const [view, setViewState] = useState<View>(() => {
    const h = window.location.hash.replace(/^#\/?/, "");
    return VALID_VIEWS.includes(h as View) ? (h as View) : "agents";
  });
  const [resourceKind, setResourceKind] = useState<ResourceKind>("model");
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [currentAgentId, setCurrentAgentId] = useState("");
  const [detailAgentId, setDetailAgentId] = useState("");
  const [editingAgentId, setEditingAgentId] = useState("");
  const [workspace, setWorkspace] = useState<{ name?: string; path?: string } | null>(null);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [chatMounted, setChatMounted] = useState(view === "conversations");
  const [runPanelOpen, setRunPanelOpen] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const [railExpandedPreference, setRailExpandedPreference] = useState<boolean | null>(readNavigationRailPreference);

  useEffect(() => {
    document.body.classList.toggle("create-mode", view === "create");
    return () => document.body.classList.remove("create-mode");
  }, [view]);

  useEffect(() => {
    const syncViewFromHash = () => {
      const hashView = window.location.hash.replace(/^#\/?/, "") as View;
      if (VALID_VIEWS.includes(hashView)) setViewState(hashView);
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
    window.history.replaceState(null, "", `#/${v}`);
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

  useEffect(() => {
    apiFetch("/api/v1/system/bootstrap").then(r => r.json()).then(d => {
      setWorkspace(d.workspace || null);
      setRuntimeReady(Boolean(d.workspace));
    }).catch(() => setRuntimeReady(false));
  }, [refreshTick]);

  const currentAgent = agents.find(a => a.metadata.id === currentAgentId);
  const runtimeType = (currentAgent as any)?.spec?.runtime?.type
    || currentAgent?.metadata.labels?.["agentkit.ksyun.com/framework"]
    || "";
  const runtimeState = runtimeReady ? "Ready" : "Connecting";

  function switchAgent(id: string) {
    if (!id) return;
    setCurrentAgentId(id);
    if (view === "conversations") setChatMounted(true);
  }

  function enterChat(agentId?: string) {
    const id = agentId || currentAgentId || agents[0]?.metadata.id || "";
    if (!id) { openCreate(); return; }
    setCurrentAgentId(id);
    setChatMounted(true);
    setView("conversations");
  }

  function openDetail(agentId: string) {
    setEditingAgentId("");
    setDetailAgentId(agentId);
    setCurrentAgentId(agentId);
    setView("agent-detail");
  }

  function openCreate() {
    setEditingAgentId("");
    setView("create");
  }

  function openEdit(agentId: string) {
    setEditingAgentId(agentId);
    setCurrentAgentId(agentId);
    setView("create");
  }

  function openResources(kind: ResourceKind) {
    setResourceKind(kind);
    setView("resources");
  }

  const breadcrumbParent = view === "create" || view === "agent-detail" ? "Agent" : null;
  const breadcrumbTitle = VIEW_TITLE[view];

  const workspaceName = workspace?.name || "Workspace";
  const workspacePath = workspace?.path || (runtimeReady ? "本地工作区" : "正在连接本地工作区");
  const focusedView = view === "create"
    || view === "conversations"
    || view === "observability";
  const railCanExpand = viewportMode !== "compact";
  const railExpanded = railCanExpand && (railExpandedPreference ?? false);

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
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <div className="app-main">
        <header className={`global-header${breadcrumbParent ? " nested" : ""}`}>
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
            <div className="breadcrumb" aria-label="当前位置">
              <span className="muted">{breadcrumbParent}</span>
              <ChevronDown size={13} style={{ transform: "rotate(-90deg)", color: "var(--text-tertiary)" }} />
              <span>{breadcrumbTitle}</span>
            </div>
          )}
          <div className="header-spacer" />
          <div className="global-context" aria-label="全局工作上下文">
            <div className="context-field">
              <span>Agent</span>
              <StudioSelect
                ariaLabel="切换当前 Agent"
                value={currentAgentId}
                placeholder="未选择"
                options={agents.map(agent => ({ value: agent.metadata.id, label: agent.metadata.name }))}
                onValueChange={switchAgent}
              />
            </div>
            <div className="context-field">
              <span>目标</span>
              <StudioSelect
                ariaLabel="切换执行目标"
                value="local"
                options={[
                  { value: "local", label: "Local" },
                  { value: "cloud-unconnected", label: "金山云 · 未连接", disabled: true },
                ]}
                onValueChange={() => undefined}
              />
            </div>
            <span className="runtime-badge">{runtimeType ? `${runtimeType} RuntimeAdapter` : "Runtime 未选择"}</span>
          </div>
          <button className="runtime-indicator" type="button" disabled={!runtimeReady}>
            <span className={`status-dot ${runtimeReady ? "success" : "warning"}`} />
            <span>Local</span>
            <span className="runtime-state">{runtimeState}</span>
            <ChevronDown size={13} />
          </button>
          <button className="icon-button tertiary" type="button" aria-label="刷新" title="刷新" onClick={() => setRefreshTick(t => t + 1)}>
            <RefreshCw size={16} />
          </button>
          {view === "conversations" && chatMounted && currentAgentId && (
            <button className="icon-button tertiary" type="button" aria-label="运行详情" title="运行详情" onClick={() => setRunPanelOpen(v => !v)}>
              <PanelRight size={16} />
            </button>
          )}
        </header>

        <main id="mainContent">
          {/* 会话页常驻挂载（display 切换），来回切换不重建工作台 */}
          <div className="chat-wrap" data-scroll-mode="workbench" style={{ display: view === "conversations" ? "flex" : "none" }}>
            <div className="chat-host">
              {chatMounted && currentAgentId && (
                <ChatWorkspace
                  key={currentAgentId}
                  agentId={currentAgentId}
                  agentName={currentAgent?.metadata.name || "Agent"}
                  agentAppearance={currentAgent?.metadata.appearance}
                />
              )}
              {chatMounted && !currentAgentId && (
                <div className="empty-state chat-agent-empty" role="status">
                  <span className="empty-icon"><Bot /></span>
                  <h2>{agentsLoaded ? "先创建 Agent 才能开始会话" : "正在载入 Agent"}</h2>
                  <p>{agentsLoaded ? "会话会使用当前 Agent 的模型、工具与运行时配置。" : "正在同步本地工作区…"}</p>
                  {agentsLoaded && <button className="primary-button" type="button" onClick={openCreate}>创建 Agent</button>}
                </div>
              )}
            </div>
            {runPanelOpen && chatMounted && currentAgentId && (
              <ChatRunPanel agentId={currentAgentId} onClose={() => setRunPanelOpen(false)} onOpenTrace={() => setView("observability")} />
            )}
          </div>

          <div style={{ display: view === "conversations" ? "none" : undefined }}>
            {view === "agents" && (
              <AgentsPage
                agents={agents}
                runtimeReady={runtimeReady}
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
            {view === "resources" && <ResourcesPage kind={resourceKind} onKindChange={setResourceKind} refreshTick={refreshTick} />}
            {view === "builds" && <BuildsPage currentAgentId={currentAgentId} agents={agents} onSelectAgent={setCurrentAgentId} onCreate={openCreate} />}
            {view === "deployments" && <DeploymentsPage onCreate={openCreate} />}
            {view === "observability" && (
              <ObservabilityPage refreshTick={refreshTick} />
            )}
            {view === "runtime-resources" && <RuntimeResourcesPage refreshTick={refreshTick} />}
            {view === "orchestration" && <OrchestrationPage currentAgentId={currentAgentId} agents={agents} onSelectAgent={setCurrentAgentId} onCreate={openCreate} />}
          </div>
        </main>
      </div>

        {settingsOpen && (
          <SettingsOverlay
            themePreference={studioTheme.preference}
            onThemePreferenceChange={studioTheme.setPreference}
            onClose={() => setSettingsOpen(false)}
          />
        )}
        <ToastRegion />
      </div>
    </>
  );
}
