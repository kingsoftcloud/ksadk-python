import { useCallback, useEffect, useMemo, useState } from "react";
import { Cpu, Wrench, Network, Sparkles, Search, ArrowRight } from "lucide-react";
import { apiFetch } from "../api";
import { PageHeaderTools } from "../components/PageHeaderPortal";

interface ResItem {
  resourceId: string; kind: string; name: string; displayName: string;
  version: string; status: string; source: string;
  requiredSecretRefs?: string[]; contract?: any;
}

interface TraceOverview {
  total?: number;
  buckets?: Array<{ startedAt: string; runs: number; completed: number }>;
}

interface DeploymentReceipt {
  status?: string;
}

const GROUPS: Array<{ kind: "model" | "tool" | "mcp" | "skill"; label: string; icon: any }> = [
  { kind: "model", label: "模型", icon: Cpu },
  { kind: "tool", label: "Tool", icon: Wrench },
  { kind: "mcp", label: "MCP Server", icon: Network },
  { kind: "skill", label: "Skill", icon: Sparkles },
];

function statusLabel(status: string): string {
  if (status === "ready") return "可用";
  if (status === "missing-secret") return "缺少凭证";
  if (status === "unhealthy" || status === "failed") return "异常";
  if (status === "unresolved") return "未解析";
  return status || "未知";
}

function credentialRef(item: ResItem): string {
  return item.requiredSecretRefs?.[0] || item.contract?.credentialRef || "";
}

export function RuntimeResourcesPage({ refreshTick, onOpenResources }: {
  refreshTick: number;
  onOpenResources: (kind: "model" | "tool" | "mcp" | "skill") => void;
}) {
  const [items, setItems] = useState<ResItem[]>([]);
  const [credStatus, setCredStatus] = useState<Record<string, any>>({});
  const [runs, setRuns] = useState(0);
  const [workspacePath, setWorkspacePath] = useState("");
  const [ready, setReady] = useState<boolean | null>(null);
  const [range, setRange] = useState<"24h" | "7d">("24h");
  const [overview, setOverview] = useState<TraceOverview | null>(null);
  const [deploymentReceipts, setDeploymentReceipts] = useState<DeploymentReceipt[]>([]);
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    const [resources, models, runList, bootstrap, traceOverview, deployments] = await Promise.all([
      apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()).catch(() => ({ items: [] })),
      apiFetch("/api/v1/catalog/models").then(r => r.json()).catch(() => null),
      apiFetch("/api/v1/runs?limit=200").then(r => r.json()).catch(() => ({ items: [] })),
      apiFetch("/api/v1/system/bootstrap").then(r => r.json()).catch(() => null),
      apiFetch(`/api/v1/traces/overview?range=${range}`).then(r => r.ok ? r.json() : null).catch(() => null),
      // This deliberately lists receipts only.  The deployment page owns
      // explicit Server refreshes, so opening Runtime Resources cannot imply
      // a cloud health check that did not happen.
      apiFetch("/api/v1/deployments").then(r => r.ok ? r.json() : { items: [] }).catch(() => ({ items: [] })),
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
    setOverview(traceOverview);
    setDeploymentReceipts(Array.isArray(deployments?.items) ? deployments.items : []);
    const refs = [...new Set(all.filter(i => i.kind === "model").map(credentialRef).filter(Boolean))];
    const entries = await Promise.all(refs.map(async (ref): Promise<[string, any]> => {
      try {
        const r = await apiFetch(`/api/v1/credentials/${encodeURIComponent(ref.replace(/^env:\/\//, ""))}`);
        return [ref, await r.json()];
      } catch { return [ref, { configured: false }]; }
    }));
    setCredStatus(Object.fromEntries(entries));
  }, [range]);

  useEffect(() => { load(); }, [load, refreshTick]);

  const effectiveStatus = (item: ResItem) => {
    if (item.kind !== "model") return item.status;
    return credStatus[credentialRef(item)]?.configured ? "ready" : "missing-secret";
  };

  const models = items.filter(i => i.kind === "model");
  const readyModels = models.filter(i => effectiveStatus(i) === "ready");
  const capabilities = items.filter(i => ["tool", "mcp", "skill"].includes(i.kind));
  const readyCapabilities = capabilities.filter(i => effectiveStatus(i) === "ready");
  const runtimeState = ready == null ? "pending" : ready ? "ready" : "failed";
  const runtimeLabel = ready == null ? "检查中" : ready ? "运行正常" : "连接失败";
  const buckets = overview?.buckets || [];
  const peak = Math.max(1, ...buckets.map(bucket => bucket.runs));
  const peakBucket = buckets.find(bucket => bucket.runs === peak);
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const problemCount = useMemo(() => items.filter(item => effectiveStatus(item) !== "ready").length, [credStatus, items]);
  const readyDeployments = deploymentReceipts.filter(item => item.status === "READY").length;
  const pendingDeployments = deploymentReceipts.filter(item => ["ADMITTING", "DEPLOYING"].includes(String(item.status))).length;
  const deploymentState = readyDeployments ? "ready" : pendingDeployments ? "pending" : "idle";
  const deploymentLabel = readyDeployments ? `${readyDeployments} 已就绪` : pendingDeployments ? `${pendingDeployments} 部署中` : "尚未部署";

  return (
    <div className="page-container runtime-resource-page" data-layout="document">
      <PageHeaderTools>
        <div className="segmented-control compact" role="tablist" aria-label="运行趋势时间范围">
          <button type="button" role="tab" aria-selected={range === "24h"} onClick={() => setRange("24h")}>24 小时</button>
          <button type="button" role="tab" aria-selected={range === "7d"} onClick={() => setRange("7d")}>7 天</button>
        </div>
      </PageHeaderTools>
      <div className="data-page-body">
        <div className="stat-strip">
        <div>
          <span className="stat-label">端侧 Runtime</span>
          <strong className="stat-value" data-state={runtimeState}>{runtimeLabel}</strong>
          <small className="stat-foot">{workspacePath || "本地工作区"}</small>
        </div>
        <div>
          <span className="stat-label">云端实例</span>
          <strong className="stat-value" data-state={deploymentState}>{deploymentLabel}</strong>
          <small className="stat-foot">Studio deployment receipt</small>
        </div>
        <div>
          <span className="stat-label">可用模型</span>
          <strong className="stat-value">{readyModels.length}</strong>
          <small className="stat-foot">Model Profile</small>
        </div>
        <div>
          <span className="stat-label">能力资源</span>
          <strong className="stat-value">{readyCapabilities.length}</strong>
          <small className="stat-foot">Tool · MCP · Skill</small>
        </div>
        <div className="emphasis">
          <span className="stat-label">运行记录</span>
          <strong className="stat-value">{overview?.total ?? runs}</strong>
          <small className="stat-foot">{range === "24h" ? "近 24 小时" : "近 7 天"}</small>
        </div>
        </div>

        <section className="runtime-trend block">
          <div className="block-head">
            <strong>运行量趋势</strong><span>{range === "24h" ? "按小时聚合" : "按天聚合"}</span>
            {peakBucket && <span className="head-actions"><span className="tag">峰值 {peak}</span></span>}
          </div>
          {buckets.length > 1 ? (
            <>
              <div className="runtime-trend-bars" aria-label="运行量趋势">
                {buckets.map((bucket, index) => (
                  <span
                    key={`${bucket.startedAt}-${index}`}
                    className="runtime-trend-bar"
                    data-peak={bucket.runs === peak || undefined}
                    style={{ height: `${Math.max(4, Math.round(bucket.runs / peak * 100))}%` }}
                    title={`${bucket.startedAt} · ${bucket.runs} 次运行`}
                  />
                ))}
              </div>
              <div className="chart-axis"><span>开始</span><span>{range === "24h" ? "12:00" : "中段"}</span><span>现在</span></div>
            </>
          ) : <div className="runtime-trend-empty"><strong>趋势数据不足</strong><span>{buckets.length === 1 ? "已有 1 个数据点，继续运行后会形成趋势。" : "运行一次 Agent 后，这里会显示真实聚合结果。"}</span></div>}
        </section>

        <section className="runtime-resource-section block">
        <div className="section-heading">
          <div><h2>本地能力概览</h2><p>优先展示异常资源，每类最多展示 5 项。</p></div>
          <span className="badge" data-state={problemCount ? "warning" : "ready"}>{problemCount ? `${problemCount} 项需处理` : "全部可用"}</span>
        </div>
        <div className="section-toolbar runtime-resource-toolbar">
          <div className="search-field">
            <Search size={14} />
            <input type="search" aria-label="搜索运行资源" placeholder="搜索资源" value={search} onChange={event => setSearch(event.target.value)} />
          </div>
        </div>
        <div className="runtime-resource-groups">
          {GROUPS.map(g => {
            const allList = items.filter(i => i.kind === g.kind);
            const list = allList
              .filter(item => !normalizedSearch || `${item.displayName} ${item.name}`.toLocaleLowerCase().includes(normalizedSearch))
              .sort((left, right) => Number(effectiveStatus(left) === "ready") - Number(effectiveStatus(right) === "ready"))
              .slice(0, 5);
            const Icon = g.icon;
            return (
              <article key={g.kind} className="runtime-resource-group block">
                <header>
                  <span className="runtime-group-icon"><Icon size={15} /></span>
                  <div><strong>{g.label}</strong><small>{allList.length} 个已发现</small></div>
                  <button className="text-button" type="button" onClick={() => onOpenResources(g.kind)}>
                    查看全部 <ArrowRight size={13} />
                  </button>
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
                        <span className="badge" data-state={status === "ready" ? "ready" : "pending"}>
                          {statusLabel(status)}
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
