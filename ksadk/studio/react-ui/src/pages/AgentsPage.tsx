import { useMemo, useState } from "react";
import { KingIcon } from "../components/KingIcon";
import { AgentAvatar, type AgentAppearance } from "../components/AgentAvatar";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { MoreActionsMenu } from "../components/MoreActionsMenu";
import { PageHeaderActions } from "../components/PageHeaderPortal";
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

function isDeclarativeAgent(agent: AgentItem): boolean {
  // The Studio-owned Codex runtime is deployed as a ManagedRuntime: its
  // delivery record fingerprints YAML, it is not a user code bundle.
  return agent.spec?.runtime?.type === "codex";
}

export function AgentsPage({ agents, runtimeReady, runtimeChecked = true, workspaceName, onCreate, onDetail, onChat, onBuild, onChanged }: {
  agents: AgentItem[];
  runtimeReady: boolean;
  runtimeChecked?: boolean;
  workspaceName: string;
  onCreate: () => void;
  onDetail: (id: string) => void;
  onChat: (id: string) => void;
  onBuild: (id: string) => void;
  onChanged: () => void;
}) {
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [pendingDelete, setPendingDelete] = useState<AgentItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState("");

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
      minWidth: 220,
      className: "agent-name-column",
      headerClassName: "agent-name-column",
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
      header: "运行时",
      minWidth: 100,
      className: "agent-runtime-column",
      headerClassName: "agent-runtime-column",
      cell: agent => {
        const runtimeType = agent.spec?.runtime?.type
          || agent.metadata.labels?.["agentkit.ksyun.com/framework"]
          || "adk";
        return <span className="tag mono">{runtimeType}</span>;
      },
    },
    {
      id: "capabilities",
      header: "能力",
      minWidth: 170,
      className: "agent-capabilities-column",
      headerClassName: "agent-capabilities-column",
      cell: agent => {
        const bindings = agent.spec?.bindings || {};
        return (
          <div className="resource-counts">
            {bindings.tools?.length ? <span>{bindings.tools.length} Tool</span> : null}
            {bindings.mcpServers?.length ? <span>{bindings.mcpServers.length} MCP</span> : null}
            {bindings.skills?.length ? <span>{bindings.skills.length} Skill</span> : null}
            {!bindings.tools?.length && !bindings.mcpServers?.length && !bindings.skills?.length && <span className="resource-count-empty">—</span>}
          </div>
        );
      },
    },
    { id: "revision", header: "版本", width: 84, className: "agent-revision-column", headerClassName: "agent-revision-column", cell: agent => <span className="mono">r{agent.metadata.revision}</span> },
    {
      id: "build",
      header: "最近校验 / 构建",
      width: 108,
      className: "agent-build-column",
      headerClassName: "agent-build-column",
      cell: agent => agent.builds?.some(build => build.status === "SUCCEEDED")
        ? <span className="badge" data-state="ready">{isDeclarativeAgent(agent) ? "声明已校验" : "已构建"}</span>
        : <span className="badge" data-state="idle">草稿</span>,
    },
    {
      id: "actions",
      header: "操作",
      minWidth: 108,
      className: "actions-column agent-actions-column",
      headerClassName: "actions-column agent-actions-column",
      cell: agent => (
        <div className="row-actions">
          <button className="button secondary small" type="button" onClick={() => onChat(agent.metadata.id)}>会话</button>
          <MoreActionsMenu
            label={`${agent.metadata.name} 的更多操作`}
            items={[
              { label: "配置", onSelect: () => onDetail(agent.metadata.id) },
              { label: isDeclarativeAgent(agent) ? "校验声明" : "构建", onSelect: () => onBuild(agent.metadata.id) },
              { label: "删除", danger: true, onSelect: () => setPendingDelete(agent) },
            ]}
          />

        </div>
      ),
    },
  ], [onBuild, onChat, onDetail]);

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
    <div className="page-container agents-page" data-layout="data">
      <PageHeaderActions>
        <button className="button accent" type="button" disabled={!runtimeReady} onClick={onCreate}>
          <KingIcon name="add" size={16} /><span>创建 Agent</span>
        </button>
      </PageHeaderActions>

      <div className="data-page-body table-data-body">
        {actionError && <div className="form-error" style={{ marginBottom: 16 }}>{actionError}</div>}

        {runtimeChecked && !runtimeReady && (
          <div className="compact-status-alert" role="alert">
            <strong>本地 Runtime 连接失败</strong>
            <span>请确认本地服务正在运行，然后刷新页面。</span>
          </div>
        )}

        <section className="agents-catalog-section block" aria-labelledby="agents-catalog-title">
          <header className="agents-catalog-header">
            <h2 id="agents-catalog-title" className="sr-only">Agent 列表</h2>
            <div className="agents-catalog-meta">
              <span>{filtered.length === agents.length ? `${agents.length} 个 Agent` : `${filtered.length} / ${agents.length} 个 Agent`}</span>
            </div>
          </header>
          <div className="section-toolbar">
            <div className="search-field">
              <KingIcon name="search" size={15} />
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
          </div>
          <StudioDataTable
            columns={columns}
            data={filtered}
            getRowId={agent => agent.metadata.id}
            caption="Agent 列表"
            minWidth={0}
            onRowActivate={agent => onDetail(agent.metadata.id)}
            rowAriaLabel={agent => `${agent.metadata.name} ${agent.metadata.id}`}
            empty={{
              icon: <KingIcon name="cpu" size={24} />,
              title: query || statusFilter ? "没有匹配的 Agent" : "还没有 Agent",
              description: query || statusFilter
                ? "调整搜索词或状态筛选。"
                : "创建第一个可运行的 Agent。",
              action: query || statusFilter ? (
                <button className="button secondary" type="button" onClick={() => { setQuery(""); setStatusFilter(""); }}>清除筛选</button>
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
