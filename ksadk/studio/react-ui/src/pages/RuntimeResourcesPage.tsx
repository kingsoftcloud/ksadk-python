import { useCallback, useEffect, useState } from "react";
import { Cpu, Wrench, Network, Sparkles } from "lucide-react";
import { apiFetch } from "../api";

interface ResItem {
  resourceId: string; kind: string; name: string; displayName: string;
  version: string; status: string; source: string;
  requiredSecretRefs?: string[]; contract?: any;
}

const GROUPS: Array<{ kind: string; label: string; icon: any }> = [
  { kind: "model", label: "Model Profile", icon: Cpu },
  { kind: "tool", label: "Tool", icon: Wrench },
  { kind: "mcp", label: "MCP Server", icon: Network },
  { kind: "skill", label: "Skill", icon: Sparkles },
];

function credentialRef(item: ResItem): string {
  return item.requiredSecretRefs?.[0] || item.contract?.credentialRef || "";
}

export function RuntimeResourcesPage({ refreshTick }: { refreshTick: number }) {
  const [items, setItems] = useState<ResItem[]>([]);
  const [credStatus, setCredStatus] = useState<Record<string, any>>({});
  const [runs, setRuns] = useState(0);
  const [workspacePath, setWorkspacePath] = useState("");
  const [ready, setReady] = useState<boolean | null>(null);

  const load = useCallback(async () => {
    const [resources, models, runList, bootstrap] = await Promise.all([
      apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()).catch(() => ({ items: [] })),
      apiFetch("/api/v1/catalog/models").then(r => r.json()).catch(() => null),
      apiFetch("/api/v1/runs?limit=200").then(r => r.json()).catch(() => ({ items: [] })),
      apiFetch("/api/v1/system/bootstrap").then(r => r.json()).catch(() => null),
    ]);
    let all: ResItem[] = resources.items || [];
    if (models?.items?.length) {
      all = [
        ...all.filter(i => i.kind !== "model" || i.source === "local" || i.source === "market"),
        ...models.items,
      ];
    }
    setItems(all);
    setRuns((runList.items || []).length);
    setWorkspacePath(bootstrap?.workspace?.path || "");
    setReady(Boolean(bootstrap?.workspace));
    const refs = [...new Set(all.filter(i => i.kind === "model").map(credentialRef).filter(Boolean))];
    const entries = await Promise.all(refs.map(async (ref): Promise<[string, any]> => {
      try {
        const r = await apiFetch(`/api/v1/credentials/${encodeURIComponent(ref.replace(/^env:\/\//, ""))}`);
        return [ref, await r.json()];
      } catch { return [ref, { configured: false }]; }
    }));
    setCredStatus(Object.fromEntries(entries));
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  const effectiveStatus = (item: ResItem) => {
    if (item.kind !== "model") return item.status;
    return credStatus[credentialRef(item)]?.configured ? "ready" : "missing-secret";
  };

  const models = items.filter(i => i.kind === "model");
  const readyModels = models.filter(i => effectiveStatus(i) === "ready");
  const capabilities = items.filter(i => ["tool", "mcp", "skill"].includes(i.kind));
  const readyCapabilities = capabilities.filter(i => effectiveStatus(i) === "ready");

  return (
    <div className="page-container runtime-resource-page" data-layout="data" data-scroll-mode="data">
      <header className="page-header">
        <div><h1>运行资源</h1><p>查看当前端侧 Runtime、模型和能力资源的真实就绪状态。</p></div>
      </header>
      <div className="data-page-body">
        <div className="runtime-overview-grid">
        <div className="runtime-metric edge">
          <span>端侧 Runtime</span>
          <strong>{ready == null ? "检查中" : ready ? "Ready" : "Disconnected"}</strong>
          <small>{workspacePath || "本地工作区"}</small>
        </div>
        <div className="runtime-metric cloud">
          <span>云端连接</span>
          <strong>未连接</strong>
          <small>一期保留部署入口</small>
        </div>
        <div className="runtime-metric">
          <span>可用模型</span>
          <strong>{readyModels.length}</strong>
          <small>Model Profile</small>
        </div>
        <div className="runtime-metric">
          <span>能力资源</span>
          <strong>{readyCapabilities.length}</strong>
          <small>Tool · MCP · Skill</small>
        </div>
        <div className="runtime-metric">
          <span>运行记录</span>
          <strong>{runs}</strong>
          <small>当前工作区</small>
        </div>
        </div>
        <section className="runtime-resource-section">
        <div className="section-heading"><div><h2>本地能力清单</h2><p>仅展示 Catalog 返回的资源和状态，不使用模拟硬件指标。</p></div></div>
        <div className="runtime-resource-groups">
          {GROUPS.map(g => {
            const list = items.filter(i => i.kind === g.kind);
            const Icon = g.icon;
            return (
              <article key={g.kind} className="runtime-resource-group">
                <header>
                  <span className="runtime-group-icon"><Icon size={15} /></span>
                  <div><strong>{g.label}</strong><small>{list.length} 个已发现</small></div>
                </header>
                <div className="runtime-resource-list">
                  {list.length === 0 ? (
                    <div className="runtime-resource-empty">当前工作区未发现此类资源</div>
                  ) : list.map(item => {
                    const status = effectiveStatus(item);
                    return (
                      <div key={item.resourceId} className="runtime-resource-row">
                        <span className={`resource-state ${status === "ready" ? "ready" : "warning"}`} />
                        <span><strong>{item.displayName}</strong><small>{item.version || item.source || item.name}</small></span>
                        <span className={`status-badge ${status === "ready" ? "success" : "warning"}`}>
                          {status === "ready" ? "Ready" : status}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </article>
            );
          })}
        </div>
        </section>
      </div>
    </div>
  );
}
