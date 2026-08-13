import { useEffect, useMemo, useState } from "react";
import { Plus, Search, Bot } from "lucide-react";
import { AgentAvatar, type AgentAppearance } from "../components/AgentAvatar";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  StudioDataTable,
  type StudioDataColumn,
} from "../components/ui/StudioDataTable";
import { StudioSelect } from "../components/ui/StudioSelect";
import { apiFetch } from "../api";

interface AgentItem {
  metadata: { id: string; name: string; revision?: number; labels?: Record<string, string>; appearance?: AgentAppearance };
  spec?: { bindings?: { tools?: string[]; mcpServers?: string[]; skills?: string[]; modelProfileId?: string; modelProfileIds?: string[] }; runtime?: { type?: string } };
  builds?: Array<{ id: string; status: string }>;
}

export function AgentsPage({ agents, runtimeReady, workspaceName, onCreate, onDetail, onChat, onBuild, onChanged }: {
  agents: AgentItem[];
  runtimeReady: boolean;
  workspaceName: string;
  onCreate: () => void;
  onDetail: (id: string) => void;
  onChat: (id: string) => void;
  onBuild: () => void;
  onChanged: () => void;
}) {
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [models, setModels] = useState(0);
  const [capabilities, setCapabilities] = useState(0);
  const [pendingDelete, setPendingDelete] = useState<AgentItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState("");

  useEffect(() => {
    // 合并本地/市场模型与 provider 探测模型；能力资源只计 ready。
    Promise.all([
      apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()),
      apiFetch("/api/v1/catalog/models").then(r => r.json()).catch(() => null),
    ]).then(([d, discovered]) => {
      const items = d.items || [];
      let modelItems = items.filter((i: any) => i.kind === "model");
      if (discovered?.items?.length) {
        modelItems = [
          ...modelItems.filter((i: any) => i.source === "local" || i.source === "market"),
          ...discovered.items,
        ];
      }
      setModels(modelItems.filter((i: any) => i.status === "ready").length);
      setCapabilities(items.filter((i: any) => ["tool", "mcp", "skill"].includes(i.kind) && i.status === "ready").length);
    }).catch(() => {});
  }, []);

  const filtered = useMemo(() => agents.filter(agent => {
    const built = Boolean(agent.builds?.some(b => b.status === "SUCCEEDED"));
    const q = query.trim().toLowerCase();
    const matchesQuery = !q || agent.metadata.name.toLowerCase().includes(q) || agent.metadata.id.toLowerCase().includes(q);
    const matchesStatus = !statusFilter
      || (statusFilter === "built" && built)
      || (statusFilter === "draft" && !built);
    return matchesQuery && matchesStatus;
  }), [agents, query, statusFilter]);

  const columns = useMemo<StudioDataColumn<AgentItem>[]>(() => [
    {
      id: "agent",
      header: "Agent",
      minWidth: 260,
      cell: agent => {
        const template = agent.metadata.labels?.["agentkit.ksyun.com/template"] || "blank";
        return (
          <div className="agent-cell">
            <AgentAvatar name={agent.metadata.name} appearance={agent.metadata.appearance} template={template} />
            <div className="agent-cell-copy">
              <strong>{agent.metadata.name}</strong>
              <span>{agent.metadata.id}</span>
            </div>
          </div>
        );
      },
    },
    {
      id: "template",
      header: "模板",
      minWidth: 150,
      cell: agent => {
        const template = agent.metadata.labels?.["agentkit.ksyun.com/template"] || "blank";
        const runtimeType = agent.spec?.runtime?.type
          || agent.metadata.labels?.["agentkit.ksyun.com/framework"]
          || "adk";
        return <><span className="status-badge neutral">{runtimeType}</span> <span className="meta-inline">{template === "research" ? "Research" : "Blank"}</span></>;
      },
    },
    {
      id: "capabilities",
      header: "能力",
      minWidth: 210,
      cell: agent => {
        const bindings = agent.spec?.bindings || {};
        return (
          <div className="resource-counts">
            <span>{bindings.tools?.length || 0} Tool</span>
            <span>{bindings.mcpServers?.length || 0} MCP</span>
            <span>{bindings.skills?.length || 0} Skill</span>
          </div>
        );
      },
    },
    { id: "revision", header: "Revision", width: 100, cell: agent => <span className="mono">r{agent.metadata.revision}</span> },
    {
      id: "build",
      header: "最近构建",
      width: 120,
      cell: agent => agent.builds?.some(build => build.status === "SUCCEEDED")
        ? <span className="status-badge success">已构建</span>
        : <span className="status-badge neutral">草稿</span>,
    },
    {
      id: "actions",
      header: "操作",
      minWidth: 300,
      className: "actions-column",
      headerClassName: "actions-column",
      cell: agent => (
        <>
          <button className="button tertiary small" type="button" onClick={() => onDetail(agent.metadata.id)}>配置</button>
          <button className="button tertiary small" type="button" onClick={() => onDetail(agent.metadata.id)}>编辑</button>
          <button className="button secondary small" type="button" onClick={() => onChat(agent.metadata.id)}>会话</button>
          <button className="button danger small" type="button" onClick={() => setPendingDelete(agent)}>删除</button>
        </>
      ),
    },
  ], [onChat, onDetail]);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setActionError("");
    try {
      const res = await apiFetch(`/api/v1/agents/${encodeURIComponent(pendingDelete.metadata.id)}`, { method: "DELETE" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        let msg = `删除失败（${res.status}）`;
        try { msg = JSON.parse(text)?.error?.message || msg; } catch {}
        throw new Error(msg);
      }
    } catch (e: any) { setActionError(e.message || "删除失败"); }
    setDeleting(false);
    setPendingDelete(null);
    onChanged();
  }

  return (
    <div className="page-container" data-layout="data" data-scroll-mode="data">
      <header className="page-header">
        <div>
          <h1>Agent</h1>
          <p>创建、配置和运行本地 Agent，同一 Bundle 可直接部署至云端。</p>
        </div>
        <button className="button accent" type="button" disabled={!runtimeReady} onClick={onCreate}>
          <Plus size={16} /><span>创建 Agent</span>
        </button>
      </header>

      <div className="data-page-body table-data-body">
        {actionError && <div className="form-error" style={{ marginBottom: 16 }}>{actionError}</div>}

        <div className="overview-strip">
        <div className="overview-item"><span>Agent</span><strong>{agents.length}</strong><small>当前工作区</small></div>
        <div className="overview-item"><span>可用模型</span><strong>{models}</strong><small>Model Profile</small></div>
        <div className="overview-item"><span>能力资源</span><strong>{capabilities}</strong><small>Tool · MCP · Skill</small></div>
        <div className="overview-item environment-overview">
          <span>运行环境</span>
          <strong><span className={`status-dot ${runtimeReady ? "success" : "warning"}`} /><span>{runtimeReady ? "运行正常" : "正在连接"}</span></strong>
          <small>{runtimeReady ? workspaceName || "本地构建与运行" : "正在连接本地工作区"}</small>
        </div>
        </div>

        <section className="content-section">
        <div className="section-toolbar">
          <div className="search-field">
            <Search size={15} />
            <input type="search" placeholder="搜索 Agent 名称或 ID" aria-label="搜索 Agent" value={query} onChange={e => setQuery(e.target.value)} />
          </div>
          <StudioSelect
            className="compact-select"
            ariaLabel="筛选 Agent 状态"
            value={statusFilter || "__all__"}
            options={[
              { value: "__all__", label: "全部状态" },
              { value: "built", label: "已构建" },
              { value: "draft", label: "草稿" },
            ]}
            onValueChange={value => setStatusFilter(value === "__all__" ? "" : value)}
          />
          <div className="toolbar-spacer" />
          <span className="sync-state">已同步</span>
        </div>
        <StudioDataTable
          columns={columns}
          data={filtered}
          getRowId={agent => agent.metadata.id}
          caption="Agent 列表"
          minWidth={1120}
          onRowActivate={agent => onDetail(agent.metadata.id)}
          rowAriaLabel={agent => `${agent.metadata.name} ${agent.metadata.id}`}
          empty={{
            icon: <Bot size={24} />,
            title: query || statusFilter ? "没有匹配的 Agent" : "还没有 Agent",
            description: query || statusFilter
              ? "调整搜索词或状态筛选后重试。"
              : "输入系统提示词并选择所需能力，创建第一个可在本地运行和构建的 Agent。",
            action: !query && !statusFilter ? (
              <button className="button accent" type="button" onClick={onCreate}>
                <Plus size={16} /><span>创建 Agent</span>
              </button>
            ) : undefined,
          }}
        />
        </section>
      </div>

      {pendingDelete && (
        <ConfirmDialog
          title={`确认删除 Agent「${pendingDelete.metadata.name}」？`}
          description={`Agent ID：${pendingDelete.metadata.id}。删除后其配置与 Revision 将移除，此操作不可撤销。`}
          confirmText="确认删除"
          busy={deleting}
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
