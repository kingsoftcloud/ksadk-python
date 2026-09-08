import { useCallback, useEffect, useMemo, useState } from "react";
import { Database, Brain, Sparkles, PlugZap } from "lucide-react";
import { apiFetch } from "../api";
import type { NativePluginBinding } from "./NativePluginBindings";
import { PageHeaderActions } from "./PageHeaderPortal";
import { StudioSelect } from "./ui/StudioSelect";

type PlatformResourceKind = "knowledge-base" | "memory-instance" | "skill-space";
type StudioResourceKind = "model" | "tool" | "mcp" | "skill" | PlatformResourceKind;

interface PlatformResourceItem {
  id: string;
  name: string;
  region: string;
  status: string;
  disabled: boolean;
  disableReason?: string;
}

interface PlatformResourceCatalog {
  kind: PlatformResourceKind;
  connectionRef: string | null;
  pluginRef: string;
  pluginSnapshotDigest: string;
  componentId: string;
  items: PlatformResourceItem[];
  total: number;
}

const META: Array<{
  kind: PlatformResourceKind;
  label: string;
  description: string;
  empty: string;
  icon: typeof Database;
}> = [
  { kind: "knowledge-base", label: "知识库", description: "绑定一个云端知识库，由 Agent 通过只读检索工具使用。", empty: "没有可用知识库", icon: Database },
  { kind: "memory-instance", label: "记忆库", description: "绑定一个长期记忆实例；启用 Memory 策略后用于召回与写入。", empty: "没有可用记忆实例", icon: Brain },
  { kind: "skill-space", label: "Skill Center", description: "选择云端 Skill Space。这里展示空间名称，与本地已安装 Skill 分开管理。", empty: "没有可用 Skill Space", icon: Sparkles },
];

const RESOURCE_TABS: Array<{ kind: StudioResourceKind; label: string }> = [
  { kind: "model", label: "模型" },
  { kind: "tool", label: "Tool" },
  { kind: "mcp", label: "MCP" },
  { kind: "skill", label: "本地 Skill" },
  { kind: "knowledge-base", label: "知识库" },
  { kind: "memory-instance", label: "记忆库" },
  { kind: "skill-space", label: "Skill Center" },
];

const OFFICIAL_REFS = new Set([
  "kingsoftcloud.dsh-knowledge",
  "kingsoftcloud.dsh-memory",
  "kingsoftcloud.dsh-skill-center",
]);

function pluginId(pluginRef: string): string {
  return pluginRef.replace(/^plugin:\/\//, "").replace(/@[^@]+$/, "");
}

function bindingKind(binding: NativePluginBinding): PlatformResourceKind | "" {
  const resource = (binding.config as any)?.binding?.resource;
  return resource?.kind || "";
}

async function read(response: Response, fallback: string) {
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error?.message || `${fallback}（${response.status}）`);
  return data;
}

export function PlatformResourceBindings({ value, onChange, onPendingChange }: {
  value: NativePluginBinding[];
  onChange: (value: NativePluginBinding[]) => void;
  onPendingChange?: (pending: boolean) => void;
}) {
  const [catalogs, setCatalogs] = useState<Partial<Record<PlatformResourceKind, PlatformResourceCatalog>>>({});
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const results = await Promise.all(META.map(async meta => read(
        await apiFetch(`/api/v1/platform-resources?kind=${encodeURIComponent(meta.kind)}`),
        `${meta.label}加载失败`,
      ) as Promise<PlatformResourceCatalog>));
      setCatalogs(Object.fromEntries(results.map(item => [item.kind, item])));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "平台资源加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const connectionRef = useMemo(
    () => META.map(meta => catalogs[meta.kind]?.connectionRef).find(Boolean) || null,
    [catalogs],
  );

  async function connect() {
    if (connecting) return;
    setConnecting(true);
    onPendingChange?.(true);
    setError("");
    try {
      await read(await apiFetch("/api/v1/resource-connections:bootstrap", { method: "POST" }), "平台连接失败");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "平台连接失败");
    } finally {
      setConnecting(false);
      onPendingChange?.(false);
    }
  }

  function select(kind: PlatformResourceKind, resourceId: string) {
    const catalog = catalogs[kind];
    if (!catalog?.connectionRef) {
      setError("请先连接金山云平台资源，再选择要绑定的资源。");
      return;
    }
    const retained = value.filter(binding => bindingKind(binding) !== kind);
    if (!resourceId) {
      onChange(retained);
      return;
    }
    const resource = catalog.items.find(item => item.id === resourceId);
    if (!resource || resource.disabled) return;
    const config: Record<string, unknown> = {
      schemaVersion: 1,
      binding: {
        id: `platform-${kind}`,
        connectionRef: catalog.connectionRef,
        resource: { kind, id: resource.id, region: resource.region },
        required: true,
      },
    };
    if (kind === "knowledge-base") config.retrieval = { mode: "tool", topK: 5, maxChars: 16000 };
    if (kind === "skill-space") Object.assign(config, { selectionMode: "discovery", selectedSkills: [], includePublic: false, executionMode: "outer-agent" });
    onChange([...retained, {
      ecosystem: "dsh",
      pluginRef: catalog.pluginRef,
      snapshotDigest: catalog.pluginSnapshotDigest,
      components: [catalog.componentId],
      enabled: true,
      config,
    }]);
  }

  const officialBindings = value.filter(binding => OFFICIAL_REFS.has(pluginId(binding.pluginRef)));
  return <div className="platform-resource-bindings">
    <div className="platform-resource-heading">
      <div><strong>平台资源</strong><span>资源选择写入 Agent Revision，构建时锁定连接、资源 ID 与插件摘要。</span></div>
      {!connectionRef && <button className="button secondary small" type="button" disabled={connecting} onClick={() => void connect()}><PlugZap size={14} />{connecting ? "连接中…" : "连接金山云"}</button>}
    </div>
    {loading ? <p role="status">正在加载知识库、记忆库和 Skill Center…</p> : META.map(meta => {
      const catalog = catalogs[meta.kind];
      const selected = officialBindings.find(binding => bindingKind(binding) === meta.kind);
      const selectedId = String((selected?.config as any)?.binding?.resource?.id || "");
      const Icon = meta.icon;
      return <div className="platform-resource-field" key={meta.kind}>
        <div className="capability-heading">
          <span className="capability-icon"><Icon size={15} /></span>
          <div><h3>{meta.label}</h3><p>{meta.description}</p></div>
        </div>
        <StudioSelect
          ariaLabel={`选择${meta.label}`}
          value={selectedId}
          placeholder={connectionRef ? `选择${meta.label}` : "请先连接金山云"}
          options={(catalog?.items || []).map(item => ({
            value: item.id,
            label: item.name,
            description: item.disabled ? item.disableReason || "当前不可绑定" : `${item.region} · ${item.status}`,
            disabled: item.disabled,
          }))}
          onValueChange={id => select(meta.kind, id)}
        />
        {!catalog?.items.length && <span className="helper">{meta.empty}</span>}
        {selectedId && <button className="text-button" type="button" onClick={() => select(meta.kind, "")}>解除绑定</button>}
      </div>;
    })}
    {error && <p className="inline-alert error" role="alert">{error}</p>}
  </div>;
}

export function PlatformResourcesPage({ kind, onKindChange, refreshTick }: {
  kind: PlatformResourceKind;
  onKindChange: (kind: StudioResourceKind) => void;
  refreshTick: number;
}) {
  const [catalog, setCatalog] = useState<PlatformResourceCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState("");
  const meta = META.find(item => item.kind === kind)!;
  const Icon = meta.icon;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setCatalog(await read(
        await apiFetch(`/api/v1/platform-resources?kind=${encodeURIComponent(kind)}`),
        `${meta.label}加载失败`,
      ));
    } catch (reason) {
      setCatalog(null);
      setError(reason instanceof Error ? reason.message : `${meta.label}加载失败`);
    } finally {
      setLoading(false);
    }
  }, [kind, meta.label]);

  useEffect(() => { void load(); }, [load, refreshTick]);

  async function connect() {
    if (connecting) return;
    setConnecting(true);
    setError("");
    try {
      await read(await apiFetch("/api/v1/resource-connections:bootstrap", { method: "POST" }), "平台连接失败");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "平台连接失败");
    } finally {
      setConnecting(false);
    }
  }

  return <div className="page-container resources-page" data-layout="data" data-scroll-mode="data">
    <PageResourceAction connected={Boolean(catalog?.connectionRef)} connecting={connecting} onConnect={() => void connect()} />
    <div className="data-page-body table-data-body">
      <div className="page-tabs" role="tablist" aria-label="资源类型">
        {RESOURCE_TABS.map(tab => <button key={tab.kind} type="button" role="tab" aria-selected={kind === tab.kind} onClick={() => onKindChange(tab.kind)}>{tab.label}{kind === tab.kind && catalog ? <span className="n">{catalog.total}</span> : null}</button>)}
      </div>
      <div className="platform-resource-heading">
        <div><strong><Icon size={16} /> {meta.label}</strong><span>{meta.description}</span></div>
        <button className="button secondary small" type="button" onClick={() => void load()}>刷新</button>
      </div>
      {!catalog?.connectionRef && !loading && <div className="inline-alert warning" role="status"><span>资源列表已从当前云账号读取。绑定到 Agent 前，请点击“连接金山云”建立只含凭据引用的本地连接声明。</span></div>}
      {error && <div className="inline-alert error" role="alert">{error}</div>}
      {loading ? <p role="status">正在加载{meta.label}…</p> : <div className="platform-resource-grid">
        {(catalog?.items || []).map(item => <article className="platform-resource-card" key={item.id}>
          <header><strong>{item.name}</strong><span className="status-badge" data-status={item.disabled ? "invalid" : "ready"}>{item.disabled ? "不可绑定" : item.status || "可用"}</span></header>
          <p>{item.region || "未返回区域"}</p>
          <p className="mono">{item.id}</p>
          {item.disableReason && <p>{item.disableReason}</p>}
        </article>)}
        {!catalog?.items.length && <div className="empty-state"><Icon size={22} /><strong>{meta.empty}</strong></div>}
      </div>}
    </div>
  </div>;
}

function PageResourceAction({ connected, connecting, onConnect }: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
}) {
  return <PageHeaderActions>
    <button className="button accent" type="button" disabled={connected || connecting} onClick={onConnect}>
      <PlugZap size={14} />{connected ? "平台已连接" : connecting ? "连接中…" : "连接金山云"}
    </button>
  </PageHeaderActions>;
}
