import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, AlertCircle, Box, CheckCircle2, CircleOff, LoaderCircle, Search, Trash2, Plus, ArrowUpRight } from "lucide-react";
import { apiFetch } from "../api";
import { TeamsAvailability } from "./TeamsAvailability";
import { DshPluginWorkspace } from "../components/DshPluginWorkspace";
import { showToast } from "../components/Toast";

interface HostState { available: boolean; version?: string | null; }
interface Capabilities { skills?: string[]; mcpServers?: string[]; hooks?: string[]; apps?: string[]; scheduledTasks?: string[]; }
interface PluginRuntimeState { state?: string; providerRef?: string | null; provider_ref?: string | null; errorCode?: string | null; error_code?: string | null; }
interface PluginPresentation { displayName?: string; shortDescription?: string; longDescription?: string; developerName?: string; category?: string; logoUrl?: string; logoUrlDark?: string; composerIconUrl?: string; defaultPrompt?: string[]; capabilities?: string[]; websiteUrl?: string; privacyPolicyUrl?: string; termsOfServiceUrl?: string; }
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
  clientExtension?: boolean;
  settingsIntegration?: boolean;
  capabilities?: Capabilities;
  interface?: PluginPresentation;
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
    clientExtension: item.clientExtension === true,
    settingsIntegration: item.settingsIntegration === true,
    capabilities: payload?.capabilities || item.capabilities,
    interface: item.interface || {},
  };
}

const keyOf = (item: InstalledPlugin) => [
  item.ecosystem,
  item.marketplaceName || "default",
  item.pluginId,
  item.resolvedVersion || "host-managed",
].join(":");
const isOfficialCodex = (item: InstalledPlugin) => item.ecosystem === 'codex' &&
  ['openai-curated', 'openai-curated-remote', 'openai-primary-runtime', 'openai-bundled'].includes(item.marketplaceName || '');
const isKsADKOfficial = (item: InstalledPlugin) => item.pluginId.startsWith('@kingsoftcloud/');
type InstalledPluginFilter = 'all' | 'ksadk' | 'dsh' | 'codex';

/** A Profile mutation replaces Core's runtime and browser session together. */
function reconnectCore() {
  if (window.__STUDIO_DSH__) window.location.reload();
}
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
  if (item.interface?.developerName) return item.interface.developerName;
  const curated = curatedPluginIdentity[item.pluginId];
  if (curated) return curated.publisher;
  const scope = item.pluginId.match(/^(@[^/]+)\//)?.[1];
  return scope || item.marketplaceName || (item.ecosystem === "dsh" ? "DeepSeek Harness" : "Codex Marketplace");
}
function pluginKind(item: InstalledPlugin) {
  const curated = curatedPluginIdentity[item.pluginId];
  if (curated) return curated.kind;
  if (item.providerRef) return "Agent Provider";
  if (item.clientExtension) return "界面扩展";
  if ((item.capabilities?.apps || []).length) return "界面扩展";
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
  const label = item.ecosystem === "codex" && item.state === "enabled" ? "已安装" : stateLabel[item.state];
  return <span className="plugin-state" data-state={item.state}><Icon size={13}/> {label}</span>;
}

function PluginUsage({ item }: { item: InstalledPlugin }) {
  const isReadyProvider = Boolean(item.providerRef && (item.ready || item.bound));
  const bindableCapabilities = [
    ...(item.capabilities?.skills || []),
    ...(item.capabilities?.mcpServers || []),
  ];
  const hasUiContribution = Boolean((item.capabilities?.apps || []).length);
  if (
    item.ecosystem !== "dsh"
    && !item.providerRef
    && bindableCapabilities.length === 0
    && !hasUiContribution
  ) return null;
  return <section className="plugin-detail-section" aria-label="贡献能力与使用方式">
    <h3>贡献能力与使用方式</h3>
    {item.providerRef && <>
      <dl className="plugin-compatibility-list"><div><dt>AgentProvider</dt><dd>{isReadyProvider ? "可用" : "未就绪"}</dd></div><div><dt>providerRef</dt><dd>{item.providerRef}</dd></div></dl>
      <p className="plugin-detail-muted">{isReadyProvider ? "在创建或编辑 Agent 时从 Runtime 选择器使用。" : "Provider 尚未就绪，暂不能用于创建 Agent。"}</p>
      {isReadyProvider && <p><a className="button secondary" href="#/create">去创建 Agent</a></p>}
    </>}
    {item.ecosystem === "dsh" && !item.providerRef && item.settingsIntegration && <p className="plugin-detail-muted">插件提供的设置页面可直接在 Studio 的插件设置中使用。</p>}
    {item.ecosystem === "dsh" && !item.providerRef && item.clientExtension && !item.settingsIntegration && <p className="plugin-detail-muted">这是 DSH Core 会话界面扩展，会在插件定义的交互状态下显示。插件未声明独立设置页，因此不会新增左侧设置标签。</p>}
    {item.ecosystem === "dsh" && !item.providerRef && !item.clientExtension && <p className="plugin-detail-muted">此插件扩展 DSH Core 运行能力，未提供独立 Studio 设置页。</p>}
    {(bindableCapabilities.length > 0 || (item.ecosystem === "codex" && hasUiContribution)) && <>
      <p className="plugin-detail-muted">{item.ecosystem === "codex" ? "在 Agent 编辑页的「能力绑定 → 绑定插件」选择此插件，保存并生成配置快照后，在新会话中使用。" : "Skill 与 MCP 能力需在 Agent 编辑页绑定后使用。"}</p>
      <p><a className="button secondary" href="#/agents">去 Agent 列表绑定</a></p>
    </>}
    {item.ecosystem === "codex" && hasUiContribution && <p className="plugin-detail-muted">此插件包含应用连接。安装和绑定会加载技能；实际操作应用还需要在运行环境中完成 Codex 账户登录及对应应用授权，模型 API Key 不代替应用授权。</p>}
  </section>;
}

function PluginIcon({ item, large = false }: { item: InstalledPlugin; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  const url = item.interface?.logoUrl || item.interface?.composerIconUrl;
  useEffect(() => setFailed(false), [url]);
  return <span className={`plugin-store-icon${large ? ' large' : ''}`}>
    {url && /^(https:\/\/|data:image\/(png|jpeg|gif|webp|svg\+xml);base64,)/.test(url) && !failed ? <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)}/> : <span>{pluginTitle(item).replace(/^@/, '').slice(0, 2).toUpperCase()}</span>}
  </span>;
}

export function PluginsPage({ refreshTick = 0 }: { refreshTick?: number }) {
  const [workspaceOpen, setWorkspaceOpen] = useState(() => new URLSearchParams(window.location.search).has('pluginSettings'));
  const [settingsPluginId, setSettingsPluginId] = useState<string | undefined>(() => new URLSearchParams(window.location.search).get('pluginSettings') || undefined);
  const [items, setItems] = useState<InstalledPlugin[]>([]);
  const [codexCatalog, setCodexCatalog] = useState<InstalledPlugin[]>([]);
  const [hosts, setHosts] = useState<Record<string, HostState | undefined>>({});
  const [selectedKey, setSelectedKey] = useState("");
  const [detail, setDetail] = useState<{ key: string; item: InstalledPlugin } | null>(null);
  const [source, setSource] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [codexAccepted, setCodexAccepted] = useState(false);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [installedFilter, setInstalledFilter] = useState<InstalledPluginFilter>('all');
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
    } catch (cause: any) { setError(cause?.message || "插件状态加载失败"); }
    finally { setBusy(""); }
  }, []);

  useEffect(() => { void load(); }, [load, refreshTick]);
  const selectedSummary = useMemo(() => [...items, ...codexCatalog].find(item => keyOf(item) === selectedKey) || null, [items, codexCatalog, selectedKey]);
  const selected = detail?.key === selectedKey && selectedSummary
    ? { ...detail.item, ...selectedSummary, capabilities: detail.item.capabilities, interface: { ...selectedSummary.interface, ...detail.item.interface } }
    : selectedSummary;
  useEffect(() => {
    if (selectedSummary?.ecosystem !== 'codex') return;
    const controller = new AbortController();
    const query = new URLSearchParams({ marketplace_name: selectedSummary.marketplaceName || '' });
    void apiFetch(`/api/v1/plugin-ecosystems/codex/plugins/${encodeURIComponent(selectedSummary.pluginId)}?${query}`, { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error(responseError(await response.json(), '插件详情加载失败'));
        const item = normalizeInstalledPlugin(await response.json());
        if (!controller.signal.aborted) setDetail({ key: selectedKey, item });
      }).catch(cause => { if (!controller.signal.aborted) setError(cause.message || '插件详情加载失败'); });
    return () => controller.abort();
  }, [selectedKey, selectedSummary]);
  const catalogGroups = useMemo(() => {
    const query = catalogQuery.trim().toLowerCase();
    const filtered = codexCatalog.filter(item => !query || [
      pluginTitle(item), pluginPublisher(item), pluginKind(item), pluginSummary(item), item.pluginId,
    ].some(value => value.toLowerCase().includes(query)));
    return pluginCategories
      .map(category => ({ category, items: filtered.filter(item => pluginCategory(item) === category) }))
      .filter(group => group.items.length > 0);
  }, [catalogQuery, codexCatalog]);
  const installedItems = useMemo(() => {
    const query = catalogQuery.trim().toLowerCase();
    return items.filter(item => {
      if (installedFilter === 'ksadk' && !isKsADKOfficial(item)) return false;
      if (installedFilter === 'dsh' && (item.ecosystem !== 'dsh' || isKsADKOfficial(item))) return false;
      if (installedFilter === 'codex' && item.ecosystem !== 'codex') return false;
      return !query || [
        pluginTitle(item), pluginPublisher(item), pluginSummary(item), item.pluginId,
      ].some(value => value.toLowerCase().includes(query));
    });
  }, [catalogQuery, installedFilter, items]);

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
      reconnectCore();
    } catch (cause: any) { setError(cause?.message || "DSH 插件安装失败"); }
    finally { setBusy(""); }
  }

  async function installCodex(item: InstalledPlugin) {
    if (!isOfficialCodex(item) && !codexAccepted) return;
    setBusy(`codex:${keyOf(item)}`); setError("");
    try {
      const response = await apiFetch(`/api/v1/plugin-ecosystems/codex/plugins/${encodeURIComponent(item.pluginId)}:install`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ marketplaceName: item.marketplaceName || null, acceptUndeclaredPermissions: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(responseError(payload, "Codex 插件安装失败"));
      const installed = normalizeInstalledPlugin(payload);
      setSelectedKey(keyOf(installed));
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
      reconnectCore();
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
      if (item.ecosystem === 'dsh') reconnectCore();
    } catch (cause: any) { setError(cause?.message || "插件卸载失败"); }
    finally { setBusy(""); }
  }

  if (workspaceOpen) return <DshPluginWorkspace pluginId={settingsPluginId} onBack={() => {
    setWorkspaceOpen(false);
    const url = new URL(window.location.href); url.searchParams.delete('pluginSettings');
    window.history.replaceState(null, '', url);
    void load();
  }}/>;

  const permissionChoice = <label className="plugin-install-consent"><input type="checkbox" checked={codexAccepted} onChange={event => setCodexAccepted(event.target.checked)}/>我信任此插件来源，允许以当前用户权限安装</label>;
  const select = (item: InstalledPlugin) => { setSelectedKey(keyOf(item)); setCodexAccepted(false); };
  const detailDescription = (selected?.interface?.longDescription || selected?.description || '').trim();
  const showDetailDescription = selected && detailDescription && detailDescription.replace(/\s+/g, ' ') !== pluginSummary(selected).replace(/\s+/g, ' ');
  return <div className="page-container plugins-page plugin-store" data-layout="document">
    {error && <p className="form-error" role="alert">{error}</p>}
    {selected ? <article className="plugin-product" aria-label="插件详情">
      <button className="plugin-back" onClick={() => { setSelectedKey(''); setCodexAccepted(false); }}><ArrowLeft size={15}/>插件</button>
      <PluginIcon item={selected} large/>
      <header><div><h2>{pluginTitle(selected)}</h2><p>{pluginSummary(selected)}</p></div>
        <div className="plugin-product-actions">
          {selected.installed ? <><PluginState item={selected}/>{selected.ecosystem === 'dsh' && <button className="button secondary" disabled={Boolean(busy)} onClick={() => void toggle(selected)}>{selected.enabled ? '停用' : '启用'}</button>}<button className="icon-button tertiary" aria-label="卸载" disabled={Boolean(busy)} onClick={() => void uninstall(selected)}><Trash2 size={16}/></button></>
            : <button className="button primary" disabled={(!isOfficialCodex(selected) && !codexAccepted) || Boolean(busy)} onClick={() => void installCodex(selected)}>{busy ? <LoaderCircle className="animate-spin" size={16}/> : <Plus size={16}/>}安装</button>}
        </div>
      </header>
      {!selected.installed && !isOfficialCodex(selected) && permissionChoice}
      {!!selected.interface?.defaultPrompt?.length && <div className="plugin-examples" aria-label="使用示例">{selected.interface.defaultPrompt.map(prompt => <p key={prompt}><span>{prompt}</span><ArrowUpRight size={16}/></p>)}</div>}
      {showDetailDescription && <p className="plugin-long-description">{detailDescription}</p>}
      {selected.ecosystem === 'dsh' && selected.enabled && !selected.providerRef && selected.settingsIntegration && <button className="button secondary" onClick={() => { setSettingsPluginId(selected.pluginId); setWorkspaceOpen(true); }}>打开插件设置<ArrowUpRight size={15}/></button>}
      {selected.failed && <p className="form-error">{selected.errorCode || '插件当前不可用'}</p>}
      <PluginUsage item={selected}/>
      <section className="plugin-product-info"><h3>信息</h3><dl>
        <div><dt>开发者</dt><dd>{pluginPublisher(selected)}</dd></div>
        <div><dt>类别</dt><dd>{selected.interface?.category || pluginKind(selected)}</dd></div>
        {!!selected.interface?.capabilities?.length && <div><dt>功能</dt><dd>{selected.interface.capabilities.join('、')}</dd></div>}
        <div><dt>版本</dt><dd>{selected.resolvedVersion || '由宿主管理'}</dd></div>
        <div><dt>生态</dt><dd>{label(selected)}</dd></div>
        {([['websiteUrl', '网站'], ['privacyPolicyUrl', '隐私政策'], ['termsOfServiceUrl', '服务条款']] as const).map(([key, title]) => selected.interface?.[key] && <div key={key}><dt>{title}</dt><dd><a href={selected.interface[key]} target="_blank" rel="noreferrer"><ArrowUpRight size={15}/><span className="sr-only">{title}</span></a></dd></div>)}
      </dl></section>
    </article> : <>
      <header className="plugins-intro"><p>为 Agent 添加工具、技能和应用。</p></header>
      <TeamsAvailability compact />
      <label className="plugin-store-search"><Search size={16}/><input aria-label="搜索插件" value={catalogQuery} onChange={event => setCatalogQuery(event.target.value)} placeholder="搜索插件"/></label>
      <section className="plugin-installed-strip"><header><h3>已安装 <small>{items.length}</small></h3><button className="plugin-text-button" onClick={() => { setSettingsPluginId(undefined); setWorkspaceOpen(true); }}>插件设置</button></header>
        <div className="plugin-installed-tabs" role="tablist" aria-label="筛选已安装插件">
          {([
            ['all', '全部'], ['ksadk', 'KsADK 官方'], ['dsh', 'DSH 插件'], ['codex', 'Codex 插件'],
          ] as const).map(([value, title]) => <button key={value} role="tab" aria-selected={installedFilter === value} onClick={() => setInstalledFilter(value)}>{title}</button>)}
        </div>
        <div className="plugin-installed-items">{installedItems.map(item => <button key={keyOf(item)} aria-label={pluginTitle(item)} title={pluginTitle(item)} onClick={() => select(item)}><PluginIcon item={item}/><span>{pluginTitle(item)}</span></button>)}</div>
        {!installedItems.length && <p className="plugin-installed-empty">当前分类没有匹配的已安装插件。</p>}
      </section>
      <div className="plugin-marketplace-tabs" role="tablist" aria-label="插件市场"><button role="tab" aria-selected={marketplaceTab === 'codex'} onClick={() => setMarketplaceTab('codex')}>Codex 插件</button><button role="tab" aria-selected={marketplaceTab === 'dsh'} onClick={() => setMarketplaceTab('dsh')}>DeepSeek Harness 插件</button></div>
      {marketplaceTab === 'codex' ? <div className="plugin-category-list" aria-label="可安装 Codex 插件">
        {busy === 'load' && <p role="status"><LoaderCircle className="animate-spin" size={16}/>正在读取插件…</p>}
        {catalogGroups.map(group => <section className="plugin-category" key={group.category}><h3>{group.category}</h3><div className="plugin-discovery-grid">
          {group.items.map(item => <button className="plugin-discovery-item" key={keyOf(item)} onClick={() => select(item)}><PluginIcon item={item}/><span><strong>{pluginTitle(item)}</strong><small>{pluginSummary(item)}</small></span><Plus size={16}/></button>)}
        </div></section>)}
        {!catalogGroups.length && busy !== 'load' && <p className="plugin-discovery-empty">{hosts.codex?.available ? '没有匹配的插件。' : 'Codex 插件服务当前不可用，请稍后刷新。'}</p>}
      </div> : <section className="plugin-source-panel"><h3>从来源添加</h3><p>输入 npm 包名安装最新版本，也可用 @版本号指定版本。</p>
        <div className="plugin-source-form"><label className="sr-only" htmlFor="dshSource">DSH 插件来源</label><input id="dshSource" value={source} onChange={event => setSource(event.target.value)} placeholder="@xmanrui/dsh-im"/><button className="button secondary" disabled={!accepted || !source.trim() || Boolean(busy)} onClick={() => void installDsh()}>{busy === 'install' ? <LoaderCircle className="animate-spin" size={15}/> : <Box size={15}/>}安装</button></div>
        <label className="plugin-install-consent"><input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)}/>我已知悉：DSH 包及安装脚本以当前系统用户权限运行。</label>
      </section>}
    </>}
  </div>;
}
