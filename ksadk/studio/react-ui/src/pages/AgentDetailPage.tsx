import { useEffect, useState } from "react";
import { SquarePen, Package, MessagesSquare, Check, ShieldCheck, CloudUpload } from "lucide-react";
import { type AgentAppearance } from "../components/AgentAvatar";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Drawer } from "../components/Drawer";
import { MoreActionsMenu } from "../components/MoreActionsMenu";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { apiFetch } from "../api";
import { CodeViewer } from "../components/ui/CodeViewer";
import {
  deploymentCreateRoute,
  deploymentDetailRoute,
  navigateToStudioHash,
} from "../studioRoutes";

interface AgentDetail {
  draft: {
    metadata: { id: string; name: string; revision: number; labels?: Record<string, string>; appearance?: AgentAppearance };
    spec: {
      description?: string;
      instructions?: { system?: string; task?: string };
      runtime?: { type?: string };
      execution?: { strategy?: string; maxSteps?: number; timeoutSeconds?: number };
      bindings?: {
        modelProfileId?: string; modelProfileIds?: string[];
        skills?: Array<{ resourceId: string }>; mcpServers?: Array<{ resourceId: string }>; tools?: Array<{ resourceId: string } | string>;
      };
    };
  };
  builds?: Array<{ id: string; status: string; bundleDigest?: string }>;
}

interface DeploymentReceipt {
  id: string;
  buildId: string;
  agentId?: string;
  instanceId?: string;
  status: "ADMITTING" | "DEPLOYING" | "READY" | "FAILED" | "ROLLED_BACK";
  target?: { region?: string };
}

function shortId(id: string, max = 28) {
  return id.length > max ? `${id.slice(0, max)}…` : id;
}

interface CatalogItem {
  resourceId: string;
  displayName: string;
  name?: string;
  contract?: { model?: string };
}

function InvocationDrawer({ detail, catalog, buildId, onClose }: {
  detail: AgentDetail;
  catalog: CatalogItem[];
  buildId?: string;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"curl" | "javascript">("curl");
  const draft = detail.draft;
  const labels = draft.metadata.labels || {};
  const modelId = draft.spec.bindings?.modelProfileId || draft.spec.bindings?.modelProfileIds?.[0] || "";
  const model = catalog.find(item => item.resourceId === modelId);
  const template = labels["agentkit.ksyun.com/template"] || "blank";
  const body = {
    model: model?.contract?.model || model?.name || labels["agentkit.ksyun.com/model"] || "glm-5.1",
    input: [{
      role: "user",
      content: [{
        type: "input_text",
        text: template === "research"
          ? "调研 Agent 工程平台的核心能力"
          : "请根据你的职责处理这个请求",
      }],
    }],
    metadata: { agent_id: draft.metadata.id },
    stream: true,
  };
  const code = tab === "curl"
    ? [
      `curl -X POST "${window.location.origin}/v1/responses" \\`,
      '  -H "Content-Type: application/json" \\',
      '  -H "Authorization: Bearer <RUNTIME_API_KEY>" \\',
      `  -d '${JSON.stringify(body, null, 2)}'`,
    ].join("\n")
    : [
      `const response = await fetch("${window.location.origin}/v1/responses", {`,
      '  method: "POST",',
      "  headers: {",
      '    "Content-Type": "application/json",',
      '    "Authorization": `Bearer ${runtimeApiKey}`',
      "  },",
      `  body: JSON.stringify(${JSON.stringify(body, null, 2)})`,
      "});",
    ].join("\n");

  return (
    <Drawer
      title="调用 Agent"
      subtitle="使用统一 Runtime 的 OpenAI Responses API；本地与云端请求体一致。"
      onClose={onClose}
    >
      <div className="callout"><ShieldCheck size={16} /><div><strong>标准接入协议</strong><p>本地 Studio 仅监听 loopback；部署后由云端网关校验 Runtime API Key，不暴露 Studio Session 或 CSRF Token。</p></div></div>
      <div className="code-tabs">
        <button className={tab === "curl" ? "active" : ""} type="button" onClick={() => setTab("curl")}>cURL</button>
        <button className={tab === "javascript" ? "active" : ""} type="button" onClick={() => setTab("javascript")}>JavaScript</button>
      </div>
      <CodeViewer
        code={code}
        language={tab === "curl" ? "bash" : "javascript"}
        filename={tab === "curl" ? "invoke-agent.sh" : "invoke-agent.js"}
        wrap
      />
      <div className="api-contract">
        <div><span>Endpoint</span><code>POST /v1/responses</code></div>
        <div><span>本地 Build</span><code>{buildId || "尚未构建"}</code></div>
        <div><span>Conversation</span><code>首次调用可省略</code></div>
      </div>
    </Drawer>
  );
}

export function AgentDetailPage({ agentId, onBack, onChat, onBuild, onEdit, onChanged }: {
  agentId: string;
  onBack: () => void;
  onChat: (id: string) => void;
  onBuild: () => void;
  onEdit: (id: string) => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [deployments, setDeployments] = useState<DeploymentReceipt[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [invocationOpen, setInvocationOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}`).then(r => r.json()).then(setDetail).catch(() => setDetail(null));
    apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()).then(d => setCatalog(d.items || [])).catch(() => {});
    apiFetch("/api/v1/deployments").then(r => r.json()).then(d => setDeployments(d.items || [])).catch(() => {});
  }, [agentId]);

  async function doDelete() {
    setDeleting(true);
    setError("");
    try {
      const res = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        throw new Error(d?.error?.message || `删除失败（${res.status}）`);
      }
      setConfirmDelete(false);
      onChanged();
      onBack();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setDeleting(false);
    }
  }

  if (!detail) {
    return <div className="page-container" data-layout="document"><p style={{ color: "var(--text-tertiary)" }}>正在加载 Agent 配置…</p></div>;
  }

  const draft = detail.draft;
  const bindings = draft.spec.bindings || {};
  const labels = draft.metadata.labels || {};
  const manifestModels = String(labels["agentkit.ksyun.com/models"] || "").split(",").map(s => s.trim()).filter(Boolean);
  const boundModelIds = bindings.modelProfileIds?.length
    ? bindings.modelProfileIds
    : bindings.modelProfileId ? [bindings.modelProfileId] : manifestModels.length ? manifestModels : labels["agentkit.ksyun.com/model"] ? [labels["agentkit.ksyun.com/model"]] : [];
  const latestBuild = (detail.builds || []).find(b => b.status === "SUCCEEDED");
  const latestDeployment = latestBuild
    ? deployments.find(deployment => deployment.buildId === latestBuild.id)
    : undefined;
  const deploymentIsReady = latestDeployment?.status === "READY";
  const nameOf = (id: string) => catalog.find(c => c.resourceId === id)?.displayName || shortId(id);
  const toolIds = (bindings.tools || []).map(t => typeof t === "string" ? t : t.resourceId);

  const groups: Array<[string, string[]]> = [
    ["Model", boundModelIds],
    ["Skill", (bindings.skills || []).map(i => i.resourceId)],
    ["MCP", (bindings.mcpServers || []).map(i => i.resourceId)],
    ["Tool", toolIds],
  ];
  const boundGroups = groups.filter(([, ids]) => ids.length > 0);

  return (
    <div className="page-container" data-layout="document">
      <PageHeaderActions>
          <button className="button accent" type="button" onClick={() => onChat(agentId)}>
            <MessagesSquare size={15} /><span>打开会话</span>
          </button>
          <button className="button secondary" type="button" onClick={() => onEdit(agentId)}>
            <SquarePen size={15} /><span>编辑</span>
          </button>
          <button className="button secondary" type="button" onClick={onBuild}>
            <Package size={15} /><span>校验并构建</span>
          </button>
          <button
            className="button secondary"
            type="button"
            onClick={() => {
              if (!latestBuild) return;
              navigateToStudioHash(latestDeployment
                ? deploymentDetailRoute(latestDeployment.id)
                : deploymentCreateRoute(latestBuild.id, draft.metadata.id));
            }}
            disabled={!latestBuild}
          >
            <CloudUpload size={15} /><span>{latestDeployment ? "查看云端部署" : "部署到云端"}</span>
          </button>
          <MoreActionsMenu
            label={`${draft.metadata.name} 的更多操作`}
            items={[
              { label: "调用方式", onSelect: () => setInvocationOpen(true) },
              { label: "删除 Agent", danger: true, onSelect: () => setConfirmDelete(true) },
            ]}
          />
      </PageHeaderActions>

      {error && <div className="form-error" style={{ marginBottom: 16 }}>{error}</div>}
      <div className="detail-layout">
        <div className="detail-main">
          <section className="detail-section block">
            <div className="section-heading"><div><h2>角色与任务</h2><p>运行时注入的系统提示词和任务契约</p></div></div>
            <div className="readonly-field"><span>系统提示词</span><pre>{draft.spec.instructions?.system || ""}</pre></div>
            <div className="readonly-field"><span>任务契约</span><pre>{draft.spec.instructions?.task || "未配置任务契约"}</pre></div>
          </section>
          <section className="detail-section block">
            <div className="section-heading"><div><h2>能力绑定</h2><p>构建时锁定 YAML 声明、版本和内容摘要</p></div></div>
            {boundGroups.length ? <div className="binding-groups">
              {boundGroups.map(([gname, ids]) => (
                <div className="binding-group" key={gname}>
                  <span>{gname}</span>
                  <div className="binding-items">
                    {ids.map(id => <span className="compact-resource" key={id}><Check size={13} />{nameOf(id)}</span>)}
                  </div>
                </div>
              ))}
            </div> : (
              <div className="capability-empty-state">
                <span>当前 Agent 尚未绑定模型、Tool、MCP 或 Skill。</span>
                <button className="button secondary small" type="button" onClick={() => onEdit(agentId)}>绑定能力</button>
              </div>
            )}
          </section>
        </div>
        <aside className="detail-aside block">
          <div className="aside-title">运行摘要</div>
          <dl>
            <div><dt>Revision</dt><dd>r{draft.metadata.revision}</dd></div>
            <div><dt>Runtime</dt><dd>{draft.spec.runtime?.type || labels["agentkit.ksyun.com/framework"] || "adk"}</dd></div>
            <div><dt>策略</dt><dd>{draft.spec.execution?.strategy || "-"}</dd></div>
            <div><dt>最大步骤</dt><dd>{draft.spec.execution?.maxSteps ?? "-"}</dd></div>
            <div><dt>超时</dt><dd>{draft.spec.execution?.timeoutSeconds ? `${draft.spec.execution.timeoutSeconds}s` : "-"}</dd></div>
          </dl>
          <div className="aside-divider" />
          <div className="build-state notice" data-state={latestBuild ? "ready" : "idle"}>
            {latestBuild ? (
              <>
                <span className="status-dot success" />
                <div><strong>部署声明已就绪</strong><span>{shortId(latestBuild.bundleDigest || latestBuild.id)}</span></div>
              </>
            ) : (
              <>
                <span className="status-dot neutral" />
                <div><strong>尚未构建</strong><span>校验 YAML 声明后即可部署或本地对话</span></div>
              </>
            )}
          </div>
          {latestDeployment && (
            <div className="build-state notice" data-state={deploymentIsReady ? "ready" : "idle"}>
              <span className={`status-dot ${deploymentIsReady ? "success" : "neutral"}`} />
              <div>
                <strong>{deploymentIsReady ? "云端实例运行中" : `云端部署：${latestDeployment.status}`}</strong>
                <span>{latestDeployment.agentId || latestDeployment.instanceId || latestDeployment.id}</span>
              </div>
            </div>
          )}
        </aside>
      </div>

      {confirmDelete && (
        <ConfirmDialog
          title={`确认删除 Agent「${draft.metadata.name}」？`}
          description={`Agent ID：${draft.metadata.id}。删除后其配置与 Revision 将移除，此操作不可撤销。`}
          confirmText="确认删除"
          busy={deleting}
          onConfirm={doDelete}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
      {invocationOpen && (
        <InvocationDrawer
          detail={detail}
          catalog={catalog}
          buildId={latestBuild?.id}
          onClose={() => setInvocationOpen(false)}
        />
      )}
    </div>
  );
}
