import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Box, CheckCircle2, CircleOff, LoaderCircle, Plug, Puzzle, RefreshCw, Search, Trash2 } from "lucide-react";
import { apiFetch } from "../api";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { studioDshCompositionHost } from "../dsh-runtime/studioDshCompositionHost";
import { requestDshUiSession } from "../dsh-runtime/dshUiSandbox";

interface HostState { available: boolean; version?: string | null; }
interface Capabilities { skills?: string[]; mcpServers?: string[]; hooks?: string[]; apps?: string[]; scheduledTasks?: string[]; }
interface PluginRuntimeState { state?: string; providerRef?: string | null; provider_ref?: string | null; errorCode?: string | null; error_code?: string | null; }
interface PluginClientBundle {
  compatible?: boolean;
  inject?: string[];
  digest?: string;
  sandboxCompatible?: boolean;
}
export type PluginLifecycleState = "installed" | "enabled" | "ready" | "bound" | "failed";
export interface InstalledPlugin {
  ecosystem: "dsh" | "codex";
  pluginId: string;
  resolvedVersion: string;
  distributionName: string;
  displayName?: string;
  marketplaceName?: string;
  installed: boolean;
  enabled: boolean;
  ready: boolean;
  bound: boolean;
  failed: boolean;
  state: PluginLifecycleState;
  providerRef?: string;
  errorCode?: string;
  riskDisclosures: string[];
  host?: HostState;
  description?: string | null;
  capabilities?: Capabilities;
  clientBundle?: PluginClientBundle | null;
}

export function normalizeInstalledPlugin(payload: any): InstalledPlugin {
  const item = payload?.item || payload;
  if (item?.ecosystem !== "dsh" && item?.ecosystem !== "codex") throw new Error("不支持的插件生态");
  const pluginId = String(item.pluginId || "").trim();
  if (!pluginId) throw new Error("插件状态缺少 pluginId");
  const runtimeState: PluginRuntimeState = item.runtimeState || item.runtime_state || {};
  const managementState = String(item.state || "").toLowerCase();
  const providerState = String(runtimeState.state || item.providerState || item.provider_state || "").toLowerCase();
  const installed = item.installed !== false;
  const failed = installed && (
    item.failed === true || managementState === "failed" || providerState === "failed"
  );
  const bound = installed && !failed && (
    item.bound === true || managementState === "bound" || providerState === "bound"
  );
  const ready = installed && !failed && (
    bound || item.ready === true || managementState === "ready" || providerState === "ready"
  );
  const enabled = installed && (
    item.enabled === true || ready || managementState === "enabled"
  );
  const state: PluginLifecycleState = failed
    ? "failed"
    : bound
      ? "bound"
      : ready
        ? "ready"
        : enabled
          ? "enabled"
          : "installed";
  const providerRef = String(runtimeState.providerRef || runtimeState.provider_ref || item.providerRef || item.provider_ref || "").trim();
  const errorCode = String(runtimeState.errorCode || runtimeState.error_code || item.errorCode || item.error_code || "").trim();
  return {
    ecosystem: item.ecosystem,
    pluginId,
    resolvedVersion: String(item.resolvedVersion || ""),
    distributionName: String(item.distributionName || pluginId),
    displayName: item.displayName,
    marketplaceName: item.marketplaceName,
    installed,
    enabled,
    ready,
    bound,
    failed,
    state,
    providerRef: providerRef || undefined,
    errorCode: errorCode || undefined,
    riskDisclosures: Array.isArray(item.riskDisclosures) ? item.riskDisclosures : [],
    host: item.host,
    description: payload?.description || item.description,
    capabilities: payload?.capabilities || item.capabilities,
    clientBundle: item.clientBundle || item.client_bundle,
  };
}

const keyOf = (item: InstalledPlugin) => [
  item.ecosystem,
  item.marketplaceName || "default",
  item.pluginId,
  item.resolvedVersion || "host-managed",
].join(":");
const label = (item: InstalledPlugin) => item.ecosystem === "dsh" ? "DeepSeek Harness 插件" : "Codex 官方插件";
const curatedPluginIdentity: Record<string, { title: string; publisher: string; kind: string; summary: string }> = {
  "@kingsoftcloud/ksadk-codex-provider": {
    title: "Codex AgentProvider",
    publisher: "KsADK 官方",
    kind: "Agent Provider",
    summary: "让 Agent 使用 Codex App Server 的原生会话、工具与审批能力。",
  },
};
function pluginTitle(item: InstalledPlugin) {
  const curated = curatedPluginIdentity[item.pluginId];
  if (curated) return curated.title;
  if (item.displayName?.trim()) return item.displayName.trim();
  const name = item.pluginId.split("/").at(-1) || item.pluginId;
  return name.replace(/^(ksadk-|dsh-)/, "").split(/[-_.]+/).map(part => part ? `${part[0].toUpperCase()}${part.slice(1)}` : "").join(" ");
}
function pluginPublisher(item: InstalledPlugin) {
  const curated = curatedPluginIdentity[item.pluginId];
  if (curated) return curated.publisher;
  const scope = item.pluginId.match(/^(@[^/]+)\//)?.[1];
  return scope || item.marketplaceName || (item.ecosystem === "dsh" ? "DeepSeek Harness" : "Codex Marketplace");
}
function pluginKind(item: InstalledPlugin) {
  const curated = curatedPluginIdentity[item.pluginId];
  if (curated) return curated.kind;
  if (item.providerRef) return "Agent Provider";
  if (item.clientBundle?.compatible || (item.capabilities?.apps || []).length) return "界面扩展";
  if ((item.capabilities?.skills || []).length || (item.capabilities?.mcpServers || []).length) return "Agent 能力";
  return item.ecosystem === "dsh" ? "Harness 扩展" : "工作台扩展";
}
function pluginSummary(item: InstalledPlugin) {
  const curated = curatedPluginIdentity[item.pluginId];
  if (curated) return curated.summary;
  if (item.description?.trim()) return item.description.trim();
  if (item.providerRef) return "为 Studio Agent 提供可选运行时与执行能力。";
  return item.ecosystem === "dsh"
    ? "通过 DeepSeek Harness 扩展 Agent 的上下文、工具或工作流。"
    : "通过 Codex App Server 扩展工作台能力。";
}
const pluginCategories = ["常用", "效率", "创意", "开发", "更多"] as const;
type PluginCategory = typeof pluginCategories[number];
function pluginCategory(item: InstalledPlugin): PluginCategory {
  const identity = `${item.pluginId} ${pluginTitle(item)}`.toLowerCase();
  if (/gmail|google-drive|google-calendar|github|notion|slack/.test(identity)) return "常用";
  if (/linear|atlassian|calendar|outlook|teams|sharepoint|clickup|monday|granola|todo|asana|trello/.test(identity)) return "效率";
  if (/canva|figma|design|image|video|remotion|hyperframe|higgs|adobe|runway/.test(identity)) return "创意";
  if (/github|cloudflare|sentry|vercel|circleci|coderabbit|security|build|developer|code/.test(identity)) return "开发";
  return "更多";
}
const responseError = (payload: any, fallback: string) => payload?.error?.message || payload?.detail || payload?.message || fallback;
const stateLabel: Record<PluginLifecycleState, string> = {
  installed: "待启用",
  enabled: "已启用",
  ready: "就绪",
  bound: "已绑定",
  failed: "失败",
};

function PluginState({ item }: { item: InstalledPlugin }) {
  const Icon = item.failed ? AlertCircle : item.state === "installed" ? CircleOff : CheckCircle2;
  return <span className="plugin-state" data-state={item.state}><Icon size={13}/> {stateLabel[item.state]}</span>;
}

function PluginUsage({ item }: { item: InstalledPlugin }) {
  const isReadyProvider = Boolean(item.providerRef && (item.ready || item.bound));
  const bindableCapabilities = [
    ...(item.capabilities?.skills || []),
    ...(item.capabilities?.mcpServers || []),
  ];
  const hasUiContribution = Boolean(
    item.clientBundle?.compatible || (item.capabilities?.apps || []).length,
  );
  if (!item.providerRef && bindableCapabilities.length === 0 && !hasUiContribution) return null;
  return <section className="plugin-detail-section" aria-label="贡献能力与使用方式">
    <h3>贡献能力与使用方式</h3>
    {item.providerRef && <>
      <dl className="plugin-compatibility-list"><div><dt>AgentProvider</dt><dd>{isReadyProvider ? "可用" : "未就绪"}</dd></div><div><dt>providerRef</dt><dd>{item.providerRef}</dd></div></dl>
      <p className="plugin-detail-muted">{isReadyProvider ? "在创建或编辑 Agent 时从 Runtime 选择器使用。" : "Provider 尚未就绪，暂不能用于创建 Agent。"}</p>
      {isReadyProvider && <p><a className="button secondary" href="#/create">去创建 Agent</a></p>}
    </>}
    {hasUiContribution && <>
      <p className="plugin-detail-muted">界面扩展启用后会自动出现在插件声明的页面、侧栏或 Tab。</p>
      {item.clientBundle?.sandboxCompatible && item.clientBundle?.digest && (
        <p><button
          className="button secondary"
          onClick={async () => {
            try {
              const session = await requestDshUiSession({
                pluginId: item.pluginId,
                clientDigest: item.clientBundle!.digest!,
              });
              const route = session.extensionPoints.find(p => p.type === "studio.route");
              if (route?.path) {
                // Register the new session's extension points directly into the
                // runtime registry — the composition host's refresh() only fires
                // on graph digest changes, so a freshly created session would be
                // invisible to the router without this.
                const { studioDshRuntime } = await import("../dsh-runtime/studioDshRuntime");
                const { STUDIO_DSH_SLOTS } = await import("../dsh-runtime/studioContributions");
                for (const point of session.extensionPoints) {
                  if (point.type === "studio.route") {
                    studioDshRuntime.contributions.register(STUDIO_DSH_SLOTS.route, {
                      id: point.id,
                      path: point.path ?? "",
                      title: point.label ?? point.id,
                      workspaceTabId: point.workspaceTabId ?? "",
                    });
                  } else if (point.type === "studio.workspace.tab") {
                    studioDshRuntime.contributions.register(STUDIO_DSH_SLOTS.workspaceTab, {
                      id: point.id,
                      label: point.label ?? point.id,
                      renderer: point.renderer,
                      session,
                    });
                  }
                }
                window.location.hash = `#${route.path}`;
              }
            } catch (e) {
              console.error("打开插件界面失败:", e);
            }
          }}
        >打开插件界面</button></p>
      )}
    </>}
    {bindableCapabilities.length > 0 && <>
      <p className="plugin-detail-muted">Skill 与 MCP 能力需在 Agent 编辑页绑定后使用。</p>
      <p><a className="button secondary" href="#/agents">去 Agent 列表绑定</a></p>
    </>}
  </section>;
}

export function PluginsPage() {
  const [items, setItems] = useState<InstalledPlugin[]>([]);
  const [codexCatalog, setCodexCatalog] = useState<InstalledPlugin[]>([]);
  const [hosts, setHosts] = useState<Record<string, HostState | undefined>>({});
  const [selectedKey, setSelectedKey] = useState("");
  const [source, setSource] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [codexAccepted, setCodexAccepted] = useState(false);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [marketplaceTab, setMarketplaceTab] = useState<"codex" | "dsh">("codex");
  const [busy, setBusy] = useState("load");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setBusy("load"); setError("");
    try {
      const [dshResponse, codexResponse] = await Promise.all([
        apiFetch("/api/v1/plugin-ecosystems/dsh/plugins"),
        apiFetch("/api/v1/plugin-ecosystems/codex/plugins"),
      ]);
      const [dsh, codex] = await Promise.all([dshResponse.json(), codexResponse.json()]);
      const codexItems = (codex.items || []).map(normalizeInstalledPlugin);
      const next = [...(dsh.items || []).map(normalizeInstalledPlugin), ...codexItems.filter((item: InstalledPlugin) => item.installed)];
      setItems(next);
      setCodexCatalog(codexItems.filter((item: InstalledPlugin) => !item.installed));
      setHosts({ dsh: dsh.host, codex: codex.host });
      if (!selectedKey && next[0]) setSelectedKey(keyOf(next[0]));
    } catch (cause: any) { setError(cause?.message || "插件状态加载失败"); }
    finally { setBusy(""); }
  }, [selectedKey]);

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const selected = useMemo(() => items.find(item => keyOf(item) === selectedKey) || null, [items, selectedKey]);
  const catalogGroups = useMemo(() => {
    const query = catalogQuery.trim().toLowerCase();
    const filtered = codexCatalog.filter(item => !query || [
      pluginTitle(item), pluginPublisher(item), pluginKind(item), pluginSummary(item), item.pluginId,
    ].some(value => value.toLowerCase().includes(query)));
    return pluginCategories
      .map(category => ({ category, items: filtered.filter(item => pluginCategory(item) === category) }))
      .filter(group => group.items.length > 0);
  }, [catalogQuery, codexCatalog]);

  async function refreshDshClientGraph() {
    try {
      await studioDshCompositionHost.refresh();
    } catch (cause: any) {
      showToast("插件状态已更新", cause?.message || "界面扩展装载失败，已保留原界面");
    }
  }

  async function installDsh() {
    if (!accepted || !source.trim()) return;
    setBusy("install"); setError("");
    try {
      const response = await apiFetch("/api/v1/plugin-ecosystems/dsh/plugins:install", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: source.trim(), acceptHostPermissions: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || "DSH 插件安装失败");
      const installed = normalizeInstalledPlugin(payload);
      setSelectedKey(keyOf(installed)); setAccepted(false); setSource("");
      showToast(installed.enabled ? "插件已安装" : "已安装，待启用", installed.displayName || installed.pluginId); await load();
      await refreshDshClientGraph();
    } catch (cause: any) { setError(cause?.message || "DSH 插件安装失败"); }
    finally { setBusy(""); }
  }

  async function installCodex(item: InstalledPlugin) {
    if (!codexAccepted) return;
    setBusy(`codex:${keyOf(item)}`); setError("");
    try {
      const response = await apiFetch(`/api/v1/plugin-ecosystems/codex/plugins/${encodeURIComponent(item.pluginId)}:install`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ marketplaceName: item.marketplaceName || null, acceptUndeclaredPermissions: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(responseError(payload, "Codex 插件安装失败"));
      const installed = normalizeInstalledPlugin(payload);
      setCodexAccepted(false); showToast(installed.enabled ? "插件已安装" : "已安装，待启用", installed.displayName || installed.pluginId); await load();
    } catch (cause: any) { setError(cause?.message || "Codex 插件安装失败"); }
    finally { setBusy(""); }
  }

  async function toggle(item: InstalledPlugin) {
    if (item.ecosystem !== "dsh") return;
    const action = item.enabled ? "disable" : "enable";
    setBusy("toggle"); setError("");
    try {
      const response = await apiFetch(`/api/v1/plugin-ecosystems/dsh/plugins/${encodeURIComponent(item.pluginId)}:${action}`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(responseError(payload, "DSH 插件状态更新失败"));
      await load();
      await refreshDshClientGraph();
    } catch (cause: any) { setError(cause?.message || "DSH 插件状态更新失败"); }
    finally { setBusy(""); }
  }

  async function uninstall(item: InstalledPlugin) {
    const path = `/api/v1/plugin-ecosystems/${item.ecosystem}/plugins/${encodeURIComponent(item.pluginId)}`;
    setBusy("delete"); setError("");
    try {
      const response = await apiFetch(path, { method: "DELETE" });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(responseError(payload, "插件卸载失败"));
      }
      setSelectedKey(""); await load();
      if (item.ecosystem === "dsh") await refreshDshClientGraph();
    } catch (cause: any) { setError(cause?.message || "插件卸载失败"); }
    finally { setBusy(""); }
  }

  return <div className="page-container plugins-page" data-layout="document">
    <PageHeaderActions><button className="icon-button tertiary" aria-label="刷新插件" onClick={() => void load()}><RefreshCw size={16}/></button></PageHeaderActions>
    <header className="plugins-intro">
      <div><h2>插件</h2><p>安装、启用和使用分开管理；只有已启用的插件才会投影给匹配的 Agent Provider。</p></div>
      <div className="plugins-hosts" aria-label="插件宿主状态"><span className="plugin-bridge-badge" data-state={hosts.dsh?.available ? "available" : "unavailable"}>{hosts.dsh?.available ? `DSH ${hosts.dsh.version || ""}` : "DSH 不可用"}</span><span className="plugin-bridge-badge" data-state={hosts.codex?.available ? "available" : "unavailable"}>{hosts.codex?.available ? `Codex ${hosts.codex.version || ""}` : "Codex 不可用"}</span></div>
    </header>
    {error && <div className="form-error" role="alert">{error}</div>}
    <div className="plugins-workspace"><section className="plugin-list-panel block"><div className="section-heading"><div className="section-heading-copy"><h2>已安装</h2><p>{busy === "load" ? "正在读取" : `${items.length} 个插件`}</p></div></div><div className="plugin-list">{items.map(item => <button key={keyOf(item)} className={`plugin-list-item${keyOf(item) === selectedKey ? " selected" : ""}`} aria-current={keyOf(item) === selectedKey ? "true" : undefined} onClick={() => setSelectedKey(keyOf(item))}><span className="plugin-avatar" data-ecosystem={item.ecosystem}>{item.ecosystem === "dsh" ? <Puzzle size={17}/> : <Plug size={17}/>}</span><span className="plugin-list-identity"><strong>{pluginTitle(item)}</strong><small>{pluginPublisher(item)} · {pluginKind(item)}</small><em>{pluginSummary(item)}</em></span><PluginState item={item}/></button>)}{!busy && !items.length && <div className="plugin-list-empty"><Plug size={19}/><span>还没有安装插件</span></div>}</div></section>
      <aside className="plugin-detail-panel block" aria-label="插件详情">{selected ? <><header className="plugin-detail-header"><span className="plugin-avatar large" data-ecosystem={selected.ecosystem}>{selected.ecosystem === "dsh" ? <Puzzle size={20}/> : <Plug size={20}/>}</span><div><h2>{pluginTitle(selected)}</h2><p>{pluginPublisher(selected)} · {pluginKind(selected)}</p></div><PluginState item={selected}/></header><p className="plugin-detail-summary">{pluginSummary(selected)}</p>{selected.failed && selected.errorCode && <p className="form-error" role="status">{selected.errorCode}</p>}<PluginUsage item={selected}/><details className="plugin-technical-details"><summary>技术信息</summary><dl><div><dt>插件标识</dt><dd>{selected.pluginId}</dd></div><div><dt>版本</dt><dd>{selected.resolvedVersion || "由宿主解析"}</dd></div><div><dt>来源</dt><dd>{label(selected)}</dd></div></dl></details><div className="plugin-detail-actions">{selected.ecosystem === "dsh" && <button className="button secondary" disabled={Boolean(busy)} onClick={() => void toggle(selected)}>{selected.enabled ? "停用" : "启用"}</button>}<button className="button danger" disabled={Boolean(busy)} onClick={() => void uninstall(selected)}><Trash2 size={15}/>卸载</button></div></> : <div className="plugin-detail-empty"><Plug size={22}/><strong>选择一个插件</strong><span>从下方添加插件后，可在这里查看状态与可用能力。</span></div>}</aside>
    </div>
    <section className="plugin-marketplace block">
      <div className="plugin-marketplace-heading"><div><h2>发现插件</h2><p>{marketplaceTab === "codex" ? <><span>兼容格式 · 生命周期由 Codex App Server 管理</span><small>安装后仍需按 Agent 显式授权。</small></> : <><span>默认插件格式 · 当前 DSH Profile</span><small>安装到当前工作区，可随后启用并绑定给 Agent。</small></>}</p></div>{marketplaceTab === "codex" && <label className="plugin-search"><Search size={16}/><span className="sr-only">搜索插件</span><input value={catalogQuery} onChange={event => setCatalogQuery(event.target.value)} placeholder="搜索插件"/></label>}</div>
      <div className="plugin-marketplace-tabs" role="tablist" aria-label="插件市场"><button type="button" role="tab" aria-selected={marketplaceTab === "codex"} onClick={() => setMarketplaceTab("codex")}>Codex 插件</button><button type="button" role="tab" aria-selected={marketplaceTab === "dsh"} onClick={() => setMarketplaceTab("dsh")}>DeepSeek Harness 插件</button></div>
      {marketplaceTab === "codex" ? busy === "load" ? <div className="plugin-marketplace-loading"><LoaderCircle className="animate-spin" size={18}/><span>正在读取 Codex 插件目录…</span></div> : codexCatalog.length > 0 ? <>
        <label className={`codex-risk-confirmation${codexAccepted ? " accepted" : ""}`}><AlertCircle size={18}/><input type="checkbox" checked={codexAccepted} onChange={event => setCodexAccepted(event.target.checked)}/><span><strong>安装前确认权限</strong><small>Codex 插件由 App Server 以当前用户权限管理，请确认插件来源可信。</small></span><em>{codexAccepted ? "已确认" : "勾选后可安装"}</em></label>
        <div className="plugin-category-list" aria-label="可安装 Codex 插件">{catalogGroups.map(group => <section className="plugin-category" key={group.category}><h3>{group.category}</h3><div className="plugin-marketplace-list">{group.items.map(item => <div className="plugin-marketplace-row" key={keyOf(item)}><span className="plugin-avatar" data-ecosystem="codex"><Plug size={16}/></span><span><strong>{pluginTitle(item)}</strong><small>{pluginPublisher(item)} · {pluginKind(item)}</small><em>{pluginSummary(item)}</em></span><button className="button secondary small" disabled={!codexAccepted || Boolean(busy)} onClick={() => void installCodex(item)}>{busy === `codex:${keyOf(item)}` ? <LoaderCircle className="animate-spin" size={15}/> : "安装"}</button></div>)}</div></section>)}</div>
        {!catalogGroups.length && <p className="plugin-discovery-empty">没有匹配的插件。</p>}
      </> : <p className="plugin-discovery-empty">当前没有可安装的 Codex 插件。</p> : <div className="plugin-dsh-market"><div className="plugin-dsh-market-copy"><span className="plugin-avatar" data-ecosystem="dsh"><Puzzle size={18}/></span><div><strong>从来源安装</strong><p>支持 npm 包、Git 地址或本地路径，适合官方、私有插件与本地开发。</p></div></div><div className="plugin-source-form"><label className="sr-only" htmlFor="dshSource">DSH 插件来源</label><input id="dshSource" value={source} onChange={event => setSource(event.target.value)} placeholder="npm 包、Git 地址或本地路径"/><button className="button secondary" disabled={!accepted || !source.trim() || Boolean(busy)} onClick={() => void installDsh()}>{busy === "install" ? <LoaderCircle className="animate-spin" size={15}/> : <Box size={15}/>}安装到 Profile</button></div><label className="plugin-source-confirmation"><input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)}/><span>我已知悉：DSH 包及安装脚本以当前系统用户权限运行。</span></label></div>}
    </section>
  </div>;
}
