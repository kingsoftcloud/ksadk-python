import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import {
  Check, CircleAlert, Cpu, Database, Eye, Network, Plus, Search, Sparkles, Wrench, Zap,
} from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, type Resolver } from "react-hook-form";
import { Drawer, InlineAlert } from "../components/Drawer";
import { SkillFileBrowser } from "../components/SkillFileBrowser";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { MoreActionsMenu } from "../components/MoreActionsMenu";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { apiFetch } from "../api";
import { FileDropzone } from "../components/ui/FileDropzone";
import { FormField } from "../components/ui/FormField";
import { StudioSelect } from "../components/ui/StudioSelect";
import { PythonToolExample } from "../components/PythonToolExample";
import {
  StudioDataTable,
  type StudioDataColumn,
} from "../components/ui/StudioDataTable";
import { applyApiFieldErrors } from "../lib/formErrors";
import {
  credentialValueSchema,
  mcpSchema,
  modelProfileSchema,
  pythonToolSchema,
  type McpFormValues,
  type ModelProfileFormValues,
  type PythonToolFormValues,
  type CredentialValueFormValues,
} from "../schemas/resourceForms";
import {
  eligibleSkillIds,
  runSkillImportBatch,
  type SkillDiscoveryCandidate,
  type SkillImportResult,
  type SkillImportSummary,
} from "../skillBatchImport";

export type ResourceKind = "model" | "tool" | "mcp" | "skill";

export interface ResItem {
  resourceId: string;
  kind: ResourceKind;
  name: string;
  displayName: string;
  version: string;
  status: string;
  source: string;
  description?: string;
  category?: string;
  contract?: any;
  health?: any;
  requiredSecretRefs?: string[];
}

const KIND_META: Record<ResourceKind, { title: string; description: string; addLabel: string; headings: [string, string, string]; icon: any }> = {
  model: { title: "模型", description: "管理 Model Profile、Endpoint 和凭据引用。", addLabel: "配置模型", headings: ["发现来源", "上下文窗口", "输入模态"], icon: Cpu },
  tool: { title: "Tool", description: "管理结构化 Tool Contract、权限和审批策略。", addLabel: "添加 Python Tool", headings: ["来源", "Tool 分组", "权限 / 边界"], icon: Wrench },
  mcp: { title: "MCP", description: "连接、探测并复用 MCP Server。", addLabel: "添加资源", headings: ["来源", "版本", "说明"], icon: Network },
  skill: { title: "Skill", description: "安装版本化 Skill，并在构建时锁定内容摘要。", addLabel: "发现 Skill", headings: ["来源", "版本", "说明"], icon: Sparkles },
};

const SOURCE_LABELS: Record<string, string> = {
  provider: "模型服务 /v1/models",
  builtin: "ksadk 内置",
  local: "工作区自定义",
  market: "市场",
};
const DEFAULT_RESOURCE_PAGE_SIZE = 20;

async function errorMessage(res: Response, fallback: string): Promise<string> {
  const text = await res.text().catch(() => "");
  try { return JSON.parse(text)?.error?.message || `${fallback}（${res.status}）`; } catch { return `${fallback}（${res.status}）`; }
}

function credentialRef(item: ResItem): string {
  return item.requiredSecretRefs?.[0] || item.contract?.credentialRef || "";
}
function credentialName(ref: string): string {
  return ref.replace(/^env:\/\//, "");
}
function formatByteCount(value: number): string {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

export function ResourcesPage({ kind, onKindChange, refreshTick }: { kind: ResourceKind; onKindChange: (k: ResourceKind) => void; refreshTick: number }) {
  const [catalog, setCatalog] = useState<ResItem[]>([]);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [statusFilter, setStatusFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [sort, setSort] = useState("default");
  const [pageSize, setPageSize] = useState(DEFAULT_RESOURCE_PAGE_SIZE);
  const [pageIndex, setPageIndex] = useState(0);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([null]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [credModel, setCredModel] = useState<ResItem | null>(null);
  const [showAddModel, setShowAddModel] = useState(false);
  const [showMcp, setShowMcp] = useState(false);
  const [mcpDetail, setMcpDetail] = useState<ResItem | null>(null);
  const [viewItem, setViewItem] = useState<ResItem | null>(null);
  const [showSkill, setShowSkill] = useState(false);
  const [showTool, setShowTool] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<ResItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const requestSeq = useRef(0);

  const loadPage = useCallback(async (cursor: string | null) => {
    const seq = ++requestSeq.current;
    const params = new URLSearchParams({
      kind,
      limit: String(pageSize),
      sort,
    });
    if (deferredSearch.trim()) params.set("query", deferredSearch.trim());
    if (statusFilter) params.set("status", statusFilter);
    if (sourceFilter) params.set("source", sourceFilter);
    if (cursor) params.set("cursor", cursor);
    setLoading(true);
    setLoadError("");
    try {
      const response = await apiFetch(`/api/v1/catalog/resources?${params}`);
      if (!response.ok) throw new Error(await errorMessage(response, "资源加载失败"));
      const payload = await response.json();
      if (requestSeq.current !== seq) return;
      setCatalog(payload.items || []);
      setNextCursor(payload.nextCursor || null);
      setTotal(Number(payload.total) || 0);
    } catch (error) {
      if (requestSeq.current !== seq) return;
      setCatalog([]);
      setNextCursor(null);
      setTotal(0);
      setLoadError(error instanceof Error ? error.message : "资源加载失败");
    } finally {
      if (requestSeq.current === seq) setLoading(false);
    }
  }, [deferredSearch, kind, pageSize, sort, sourceFilter, statusFilter]);

  const resetAndLoad = useCallback(() => {
    setCursorStack([null]);
    setPageIndex(0);
    void loadPage(null);
  }, [loadPage]);

  const reloadCurrent = useCallback(() => {
    void loadPage(cursorStack[pageIndex] || null);
  }, [cursorStack, loadPage, pageIndex]);

  useEffect(() => { resetAndLoad(); }, [refreshTick, resetAndLoad]);

  const probeMcp = useCallback(async (item: ResItem) => {
    try {
      const res = await apiFetch(`/api/v1/catalog/mcp-servers/${encodeURIComponent(item.resourceId)}:probe?timeoutSeconds=15`, { method: "POST" });
      if (!res.ok) throw new Error(await errorMessage(res, "MCP 探测失败"));
      const probed = await res.json();
      reloadCurrent();
      showToast("MCP 探测完成", `已发现 ${probed.health?.toolCount || 0} 个 Tool。`);
    } catch (e: any) {
      reloadCurrent();
      showToast("MCP 探测失败", e.message, "error");
    }
  }, [reloadCurrent]);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      const res = await apiFetch(`/api/v1/catalog/resources/${encodeURIComponent(pendingDelete.resourceId)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await errorMessage(res, "删除失败"));
      setPendingDelete(null);
      if (catalog.length === 1 && pageIndex > 0) {
        const previous = pageIndex - 1;
        setPageIndex(previous);
        await loadPage(cursorStack[previous] || null);
      } else {
        await loadPage(cursorStack[pageIndex] || null);
      }
      showToast("资源已删除", `${pendingDelete.displayName || pendingDelete.name} 已从工作区移除。`);
    } catch (e: any) {
      showToast("删除失败", e.message, "error");
    }
    setDeleting(false);
  }

  function handleAdd() {
    if (kind === "model") {
      setShowAddModel(true);
      return;
    }
    if (kind === "mcp") { setShowMcp(true); return; }
    if (kind === "skill") { setShowSkill(true); return; }
    setShowTool(true);
  }

  const meta = KIND_META[kind];
  const columns = useMemo<StudioDataColumn<ResItem>[]>(() => [
    { id: "name", header: "名称", minWidth: 250, cell: item => <ResourceNameCell item={item} /> },
    { id: "source", header: meta.headings[0], minWidth: 170, cell: item => SOURCE_LABELS[item.source] || item.source },
    { id: "detail", header: meta.headings[1], minWidth: 150, cell: item => <ResourceDetailCell item={item} /> },
    { id: "capability", header: meta.headings[2], minWidth: 240, className: "capability-cell", cell: item => <ResourceCapabilityCell item={item} /> },
    { id: "status", header: "状态", minWidth: 135, cell: item => <ResourceStatusCell item={item} /> },
    {
      id: "actions",
      header: "操作",
      minWidth: 160,
      className: "actions-column",
      headerClassName: "actions-column",
      cell: item => (
        <ResourceActionsCell
          item={item}
          onConfigure={() => setCredModel(item)}
          onView={() => item.kind === "mcp" ? setMcpDetail(item) : setViewItem(item)}
          onProbe={() => probeMcp(item)}
          onDelete={() => setPendingDelete(item)}
        />
      ),
    },
  ], [meta.headings, probeMcp]);

  const previousPage = useCallback(() => {
    if (pageIndex <= 0) return;
    const previous = pageIndex - 1;
    setPageIndex(previous);
    void loadPage(cursorStack[previous] || null);
  }, [cursorStack, loadPage, pageIndex]);

  const nextPage = useCallback(() => {
    if (!nextCursor) return;
    const next = pageIndex + 1;
    setCursorStack(current => {
      const updated = current.slice(0, next);
      updated[next] = nextCursor;
      return updated;
    });
    setPageIndex(next);
    void loadPage(nextCursor);
  }, [loadPage, nextCursor, pageIndex]);

  return (
    <div className="page-container" data-layout="document">
      <PageHeaderActions>
        <button className="button accent" type="button" onClick={handleAdd}>
          <Plus size={15} /><span>{meta.addLabel}</span>
        </button>
      </PageHeaderActions>
      <div className="data-page-body table-data-body">
        <div className="page-tabs" role="tablist" aria-label="资源类型">
          {(Object.keys(KIND_META) as ResourceKind[]).map(tabKind => (
            <button
              key={tabKind}
              type="button"
              role="tab"
              aria-selected={kind === tabKind}
              onClick={() => onKindChange(tabKind)}
            >
              {KIND_META[tabKind].title}
              {kind === tabKind && <span className="n">{total}</span>}
            </button>
          ))}
        </div>
        <div className="section-toolbar">
          <div className="search-field">
            <Search size={14} />
            <input type="search" placeholder="搜索资源" value={search} onChange={e => setSearch(e.target.value)} />
          </div>
          <StudioSelect
            className="compact-select"
            ariaLabel="筛选资源状态"
            value={statusFilter || "__all__"}
            options={[
              { value: "__all__", label: "全部状态" },
              { value: "ready", label: "可用" },
              { value: "missing-secret", label: "缺少凭证" },
              { value: "unhealthy", label: "异常" },
              { value: "unresolved", label: "未解析" },
            ]}
            onValueChange={value => setStatusFilter(value === "__all__" ? "" : value)}
          />
          <StudioSelect
            className="compact-select"
            ariaLabel="筛选资源来源"
            value={sourceFilter || "__all__"}
            options={[
              { value: "__all__", label: "全部来源" },
              { value: "provider", label: "模型服务" },
              { value: "builtin", label: "ksadk 内置" },
              { value: "local", label: "工作区自定义" },
              { value: "market", label: "市场" },
            ]}
            onValueChange={value => setSourceFilter(value === "__all__" ? "" : value)}
          />
          <StudioSelect
            className="compact-select"
            ariaLabel="资源排序"
            value={sort}
            options={[
              { value: "default", label: "默认排序" },
              { value: "displayName:asc", label: "名称升序" },
              { value: "displayName:desc", label: "名称降序" },
            ]}
            onValueChange={setSort}
          />
          <StudioSelect
            className="compact-select"
            ariaLabel="每页显示数量"
            value={String(pageSize)}
            options={[
              { value: "20", label: "每页 20 条" },
              { value: "50", label: "每页 50 条" },
              { value: "100", label: "每页 100 条" },
            ]}
            onValueChange={value => setPageSize(Number(value))}
          />
        </div>
        <StudioDataTable
          columns={columns}
          data={catalog}
          getRowId={item => item.resourceId}
          caption={`${meta.title}资源列表`}
          minWidth={1180}
          loading={loading}
          error={loadError}
          onRetry={reloadCurrent}
          empty={{
            icon: <Database size={20} />,
            title: "没有匹配的资源",
            description: "调整筛选条件，或添加一个新的工程资源。",
          }}
          pagination={{
            pageIndex,
            pageSize,
            total,
            hasNextPage: Boolean(nextCursor),
            onPreviousPage: previousPage,
            onNextPage: nextPage,
          }}
        />
      </div>

      {credModel && (
        <ModelCredentialDrawer
          model={credModel}
          onClose={() => setCredModel(null)}
          onChanged={resetAndLoad}
        />
      )}
      {showAddModel && <AddModelDrawer onClose={() => setShowAddModel(false)} onAdded={() => { setShowAddModel(false); resetAndLoad(); }} />}
      {showMcp && <McpConnectDrawer onClose={() => setShowMcp(false)} onConnected={() => { setShowMcp(false); resetAndLoad(); }} />}
      {mcpDetail && <McpDetailDrawer item={mcpDetail} onClose={() => setMcpDetail(null)} />}
      {viewItem && <ResourceDetailDrawer item={viewItem} onClose={() => setViewItem(null)} />}
      {showSkill && <SkillDiscoveryDrawer onClose={() => setShowSkill(false)} onCatalogChanged={resetAndLoad} />}
      {showTool && <PythonToolDrawer onClose={() => setShowTool(false)} onAdded={() => { setShowTool(false); resetAndLoad(); }} />}
      {pendingDelete && (
        <ConfirmDialog
          title={`确认删除资源「${pendingDelete.displayName || pendingDelete.name}」？`}
          description="删除后已绑定该资源的 Agent 不会自动更新，需手动重新编辑。"
          confirmText="确认删除"
          busy={deleting}
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}

/* ================= 资源表格单元格 ================= */

function ResourceNameCell({ item }: { item: ResItem }) {
  const Icon = KIND_META[item.kind]?.icon || Database;
  return (
    <div className="agent-cell">
      <span className="capability-icon"><Icon size={15} /></span>
      <div className="agent-cell-copy"><strong>{item.displayName}</strong><span>{item.name}</span></div>
    </div>
  );
}

function ResourceDetailCell({ item }: { item: ResItem }) {
  if (item.kind === "model") {
    const metadata = item.contract?.metadata || {};
    const tokens = Number(metadata.context_window_tokens || 0);
    const origin = item.contract?.discovery?.contextWindow === "provider" ? "服务返回" : "ksadk 默认";
    const value = tokens >= 1000000
      ? `${(tokens / 1000000).toFixed(tokens % 1000000 ? 1 : 0)}M`
      : tokens >= 1000 ? `${Math.round(tokens / 1000)}K` : `${tokens || "-"}`;
    return <><strong>{value}</strong><span className="resource-origin">{origin}</span></>;
  }
  if (item.kind === "tool") {
    return <span className="tag">{item.contract?.group || item.category || "general"}</span>;
  }
  return <span className="mono">{item.version}</span>;
}

function ResourceCapabilityCell({ item }: { item: ResItem }) {
  if (item.kind === "model") {
    const capabilities = item.contract?.metadata?.capabilities || {};
    const modalities = ["文字"];
    if (capabilities.multimodal_input_image) modalities.push("图片");
    if (capabilities.multimodal_input_video) modalities.push("视频");
    if (capabilities.multimodal_input_file) modalities.push("文件");
    const origin = item.contract?.discovery?.inputModalities === "provider" ? "服务返回" : "ksadk 默认";
    return <>{modalities.join(" + ")}<span className="resource-origin">{origin}</span></>;
  }
  if (item.kind === "tool") {
    const approval = item.contract?.approval === "always" ? "需审批" : "无需审批";
    const boundary = item.contract?.boundary || "ksadk-runtime";
    return <>{approval}<span className="resource-origin">{boundary}</span></>;
  }
  const description = item.description || "未提供说明";
  return <span className="cell-clamp" title={description}>{description}</span>;
}

function ResourceStatusCell({ item }: { item: ResItem }) {
  if (item.kind === "model") {
    const configured = item.status === "ready";
    return <span className="badge" data-state={configured ? "ready" : "pending"}>{configured ? "凭证已配置" : "凭证未配置"}</span>;
  }
  const label = item.status === "ready" ? "可用"
    : item.status === "failed" || item.status === "unhealthy" ? "异常"
      : item.status === "unresolved" ? "未解析"
        : item.status === "missing-secret" ? "缺少凭证"
          : item.status;
  return <span className="badge" data-state={item.status === "ready" ? "ready" : item.status === "failed" || item.status === "unhealthy" ? "failed" : "pending"}>{label}</span>;
}

function ResourceActionsCell({ item, onConfigure, onView, onProbe, onDelete }: {
  item: ResItem;
  onConfigure: () => void;
  onView: () => void;
  onProbe: () => void;
  onDelete: () => void;
}) {
  const menuItems = item.kind === "mcp"
    ? [
      { label: "重新探测", onSelect: onProbe, disabled: item.source !== "local" },
      ...(item.source === "local" ? [{ label: "删除", danger: true, onSelect: onDelete }] : []),
    ]
    : [
      { label: "查看详情", onSelect: onView },
      ...(item.source === "local" ? [{ label: "删除", danger: true, onSelect: onDelete }] : []),
    ];

  return (
    <div className="row-actions">
      <button className="button secondary small" type="button" onClick={item.kind === "model" ? onConfigure : onView}>
        {item.kind === "model" ? "配置凭证" : "查看"}
      </button>
      <MoreActionsMenu label={`${item.displayName} 的更多操作`} items={menuItems} />
    </div>
  );
}

/* ================= 模型凭证抽屉 ================= */

export function ModelCredentialDrawer({ model, onClose, onChanged }: {
  model: ResItem;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const ref = credentialRef(model);
  const name = credentialName(ref);
  const [status, setStatus] = useState<any>(null);
  const credentialForm = useForm<CredentialValueFormValues>({
    resolver: zodResolver(credentialValueSchema) as Resolver<CredentialValueFormValues>,
    defaultValues: { value: "" },
  });
  const [error, setError] = useState<{ title: string; message: string } | null>(null);
  const [busy, setBusy] = useState<"" | "save" | "test" | "remove">("");

  useEffect(() => {
    apiFetch(`/api/v1/credentials/${encodeURIComponent(name)}`)
      .then(r => r.json()).then(setStatus)
      .catch(() => setStatus({ configured: false, source: "missing" }));
  }, [name]);

  const configured = Boolean(status?.configured);
  const source = status?.source || "missing";
  const statusTitle = status == null ? "正在检查凭证" : configured ? "模型凭证已配置" : "模型凭证未配置";
  const statusDesc = status == null
    ? "检查当前 Runtime 是否已经获得模型凭证。"
    : source === "session"
      ? "凭证已持久保存到工作区，重启后仍生效，所有 Agent 可复用。"
      : source === "environment"
        ? "凭证由 Studio 启动环境变量提供，可以用新的会话凭证临时覆盖。"
        : "输入 API Key 后即可在本地运行当前模型。";

  async function save(testConnection: boolean, values: CredentialValueFormValues) {
    setError(null);
    if (!values.value && !configured) {
      credentialForm.setError("value", { type: "manual", message: "当前模型还没有可用凭证，请输入 API Key。" });
      return;
    }
    setBusy(testConnection ? "test" : "save");
    let saved = false;
    try {
      if (values.value) {
        const res = await apiFetch(`/api/v1/credentials/${encodeURIComponent(name)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: values.value, persistence: "session" }),
        });
        if (!res.ok) throw new Error(await errorMessage(res, "凭证保存失败"));
        saved = true;
      }
      let latency: number | null = null;
      if (testConnection) {
        const res = await apiFetch(`/api/v1/model-profiles/${encodeURIComponent(model.resourceId)}:test`, { method: "POST" });
        const text = await res.text().catch(() => "");
        let d: any = {};
        try { d = JSON.parse(text); } catch {}
        if (!res.ok || d.ok === false) throw new Error(d?.error?.message || `HTTP ${res.status}`);
        latency = d.latencyMs ?? 0;
      }
      onChanged?.();
      onClose();
      showToast(
        testConnection ? "模型连接测试通过" : "模型凭证已保存",
        testConnection ? `${model.displayName} · ${latency} ms` : "凭证已持久保存到工作区，所有 Agent 可复用。",
      );
    } catch (e: any) {
      setError({
        title: saved ? "凭证已保存，但连接测试失败" : "模型凭证配置失败",
        message: e.message,
      });
      if (saved) onChanged?.();
    }
    setBusy("");
  }

  async function remove() {
    setError(null);
    setBusy("remove");
    try {
      const res = await apiFetch(`/api/v1/credentials/${encodeURIComponent(name)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await errorMessage(res, "凭证清除失败"));
      onChanged?.();
      showToast("会话凭证已清除", model.displayName);
      onClose();
    } catch (e: any) {
      setError({ title: "凭证清除失败", message: e.message });
    }
    setBusy("");
  }

  return (
    <FormProvider {...credentialForm}>
    <Drawer
      title="配置模型凭证"
      subtitle="凭证只保存在当前 Studio 会话，不写入 Agent 或 Bundle。"
      onClose={onClose}
      footer={
        <>
          {source === "session" && (
            <button className="button secondary" type="button" onClick={remove} disabled={busy !== ""}>
              {busy === "remove" ? "正在清除" : "清除会话凭证"}
            </button>
          )}
          <span className="drawer-footer-spacer" />
          <button className="button secondary" type="button" onClick={onClose}>取消</button>
          <button className="button secondary" type="button" onClick={credentialForm.handleSubmit(values => save(false, values))} disabled={busy !== ""}>
            {busy === "save" ? "正在保存" : "仅保存"}
          </button>
          <button className="button accent" type="button" onClick={credentialForm.handleSubmit(values => save(true, values))} disabled={busy !== ""}>
            <Check size={15} /><span>{busy === "test" ? "正在测试" : "保存并测试"}</span>
          </button>
        </>
      }
    >
      <div className="credential-profile">
        <div><span>模型</span><strong>{model.displayName}</strong></div>
        <div><span>Provider</span><strong>{model.contract?.provider || "openai-compatible"}</strong></div>
        <div><span>Endpoint</span><code>{model.contract?.endpointUrl || model.contract?.baseUrl || "-"}</code></div>
        <div><span>凭证引用</span><code>{ref || "-"}</code></div>
      </div>

      <div className={`credential-status ${configured ? "configured" : "missing"}`}>
        <span className={`status-dot ${status == null ? "warning" : configured ? "success" : "warning"}`} />
        <div>
          <strong>{statusTitle}</strong>
          <p>{statusDesc}</p>
        </div>
      </div>

      <FormField label="API Key" requirement={configured ? "optional" : "required"} htmlFor="modelCredValue" hint="保存后立即生效；关闭 Studio 后自动清除，需要持久化时可通过启动环境变量注入。" error={credentialForm.formState.errors.value?.message}>
        <input
          id="modelCredValue"
          type="password"
          autoComplete="new-password"
          maxLength={16384}
          placeholder={configured ? "输入新的 API Key 以覆盖当前凭证" : "输入新的 API Key"}
          {...credentialForm.register("value")}
        />
      </FormField>

      {error && <InlineAlert kind="error" title={error.title} message={error.message} />}

    </Drawer>
    </FormProvider>
  );
}

/* ================= 添加模型抽屉（智能探测） ================= */

function AddModelDrawer({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const modelForm = useForm<ModelProfileFormValues>({
    resolver: zodResolver(modelProfileSchema) as Resolver<ModelProfileFormValues>,
    defaultValues: {
      name: "",
      displayName: "",
      model: "",
      endpointUrl: "",
      credentialRef: "env://MODEL_API_KEY",
      description: "",
      apiKey: "",
      temperature: 0.2,
      maxTokens: 2048,
      addressMode: "endpoint",
      wireApi: "",
    },
  });
  const {
    name,
    displayName,
    model: modelId,
    endpointUrl: url,
    credentialRef,
    apiKey,
    temperature,
    maxTokens,
    addressMode,
    wireApi,
  } = modelForm.watch();
  const envName = credentialRef.replace(/^env:\/\//, "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [probing, setProbing] = useState(false);
  const [probeAttempts, setProbeAttempts] = useState<any[]>([]);
  const [probeModels, setProbeModels] = useState<string[]>([]);
  const [catalogModels, setCatalogModels] = useState<string[]>([]);

  useEffect(() => {
    apiFetch("/api/v1/catalog/models").then(r => r.json()).then(d => {
      setCatalogModels((d.items || []).map((i: any) => i.name).filter(Boolean));
    }).catch(() => {});
  }, []);

  async function probe() {
    if (!url.trim()) { setError("请先填写接口地址再探测"); return; }
    setProbing(true); setError(""); setProbeAttempts([]); setProbeModels([]);
    try {
      const body: any = { url: url.trim() };
      if (envName.trim()) body.credentialRef = `env://${envName.trim()}`;
      if (apiKey.trim()) body.apiKey = apiKey.trim();
      const res = await apiFetch("/api/v1/model-endpoints:probe", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await errorMessage(res, "探测失败"));
      const d = await res.json();
      setProbeAttempts(d.attempts || []);
      setProbeModels(d.models || []);
      if (d.recommended) {
        modelForm.setValue("wireApi", d.recommended.wireApi || "chat");
        modelForm.setValue("addressMode", "endpoint");
        modelForm.setValue("endpointUrl", d.recommended.endpointUrl, { shouldValidate: true });
        if (d.recommended.status === "auth_required") setError("端点可达，但需要有效凭证（401/403）；配置 API Key 后可正常使用");
      } else {
        setError("未能识别可用协议，请检查地址或网络");
      }
    } catch (e: any) { setError(e.message); }
    setProbing(false);
  }

  const modelSuggestions = [...new Set([...probeModels, ...catalogModels])];

  async function save(values: ModelProfileFormValues) {
    setSaving(true); setError("");
    try {
      const credentialName = values.credentialRef.replace(/^env:\/\//, "");
      if (values.apiKey.trim()) {
        const credRes = await apiFetch(`/api/v1/credentials/${encodeURIComponent(credentialName)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: values.apiKey.trim(), persistence: "session" }),
        });
        if (!credRes.ok) throw new Error(await errorMessage(credRes, "凭证保存失败"));
      }
      const spec: any = {
        provider: "openai-compatible",
        model: values.model.trim(),
        credentialRef: values.credentialRef,
        parameters: { temperature: values.temperature, max_tokens: values.maxTokens },
      };
      if (values.wireApi) spec.wireApi = values.wireApi;
      if (values.addressMode === "endpoint") spec.endpointUrl = values.endpointUrl.trim(); else spec.baseUrl = values.endpointUrl.trim();
      const res = await apiFetch("/api/v1/catalog/model-profiles", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: values.name.trim(), displayName: values.displayName.trim(), version: "1.0.0", description: values.description || "", spec }),
      });
      if (!res.ok) {
        const payload = await res.json().catch(() => null);
        if (applyApiFieldErrors(payload, modelForm.setError)) {
          setSaving(false);
          return;
        }
        throw new Error(payload?.error?.message || `创建失败（${res.status}）`);
      }
      showToast("模型已创建", `${values.displayName.trim()} · ${values.model.trim()}`);
      onAdded();
    } catch (e: any) { setError(e.message); }
    setSaving(false);
  }

  const attemptLabel = (a: any) =>
    `${a.protocol === "chat" ? "Chat" : "Responses"} · ${
      a.status === "ok" ? `可用 ${a.latencyMs}ms`
      : a.status === "auth_required" ? "需凭证"
      : a.status === "recognized" ? "可识别"
      : a.status === "unavailable" ? "不存在"
      : a.status === "unreachable" ? "不可达" : "错误"}`;
  const attemptState = (a: any) =>
    a.status === "ok" ? "ready" : a.status === "unavailable" || a.status === "unreachable" ? "failed" : "pending";

  return (
    <FormProvider {...modelForm}>
    <Drawer
      title="添加模型"
      subtitle="接入 OpenAI 兼容端点的自定义 Model Profile。"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="button secondary" type="button" onClick={onClose}>取消</button>
          <button className="button accent" type="button" onClick={modelForm.handleSubmit(save)} disabled={saving}>
            <Check size={15} /><span>{saving ? "创建中…" : "创建模型"}</span>
          </button>
        </>
      }
    >
      <div className="form-grid two-columns">
        <FormField label="名称（Slug）" requirement="required" htmlFor="amName" error={modelForm.formState.errors.name?.message}>
          <input id="amName" className="mono" placeholder="my-model" {...modelForm.register("name")} />
        </FormField>
        <FormField label="显示名称" requirement="required" htmlFor="amDisplayName" error={modelForm.formState.errors.displayName?.message}>
          <input id="amDisplayName" placeholder="我的模型" {...modelForm.register("displayName")} />
        </FormField>
      </div>
      <FormField label="模型 ID" requirement="required" htmlFor="amModelId" error={modelForm.formState.errors.model?.message}>
        <div>
        <input id="amModelId" className="mono" placeholder="glm-5.1 / gpt-4o-mini / …" list="am-model-suggestions" {...modelForm.register("model")} />
        <datalist id="am-model-suggestions">{modelSuggestions.map(m => <option key={m} value={m} />)}</datalist>
        {modelSuggestions.length > 0 && <span className="helper">可输入或从 {modelSuggestions.length} 个可用模型中选择（自动补全）。</span>}
        </div>
      </FormField>

      <FormField label="接口地址" requirement="required" htmlFor="amEndpoint" hint="支持主机、/v1、Chat Completions 或 Responses 地址；智能探测会自动归一化。" error={modelForm.formState.errors.endpointUrl?.message}>
        <div>
        {wireApi && <span className="tag">{wireApi === "responses" ? "Responses 协议" : "Chat 协议"}</span>}
        <div style={{ display: "flex", gap: 8, marginBottom: 10, alignItems: "center" }}>
          <div className="segmented-control">
            <button type="button" className={addressMode === "endpoint" ? "selected" : ""} onClick={() => modelForm.setValue("addressMode", "endpoint")}>完整 endpointUrl</button>
            <button type="button" className={addressMode === "base" ? "selected" : ""} onClick={() => modelForm.setValue("addressMode", "base")}>baseUrl</button>
          </div>
          <button className="button secondary small" type="button" onClick={probe} disabled={probing || !url.trim()}>
            <Zap size={14} /><span>{probing ? "探测中…" : "智能探测"}</span>
          </button>
        </div>
        <input id="amEndpoint" className="mono" placeholder={addressMode === "endpoint" ? "https://host/v1/chat/completions" : "https://host/v1"} {...modelForm.register("endpointUrl")} />
        {probeAttempts.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>
            {probeAttempts.map((a, i) => (
              <span key={i} className="badge" data-state={attemptState(a)} title={a.endpointUrl}>{attemptLabel(a)}</span>
            ))}
          </div>
        )}
        </div>
      </FormField>

      <div className="form-grid two-columns">
        <FormField label="凭证环境变量名" requirement="required" htmlFor="amEnvName" error={modelForm.formState.errors.credentialRef?.message}>
          <input
            id="amEnvName"
            className="mono"
            value={envName}
            onChange={event => modelForm.setValue("credentialRef", `env://${event.target.value}`, { shouldDirty: true, shouldValidate: true })}
            placeholder="MY_MODEL_API_KEY"
          />
        </FormField>
        <FormField label="API Key 值" requirement="optional" htmlFor="amApiKey" hint="仅保存到当前 Studio 会话；留空则从启动环境读取。" error={modelForm.formState.errors.apiKey?.message}>
          <input id="amApiKey" type="password" autoComplete="new-password" placeholder="留空则从环境读取" {...modelForm.register("apiKey")} />
        </FormField>
      </div>

      <div className="form-grid two-columns">
        <FormField label="temperature" requirement="optional" htmlFor="amTemp" error={modelForm.formState.errors.temperature?.message}>
          <input id="amTemp" type="number" step="0.1" min={0} max={2} {...modelForm.register("temperature", { valueAsNumber: true })} />
        </FormField>
        <FormField label="max_tokens" requirement="optional" htmlFor="amMaxTokens" error={modelForm.formState.errors.maxTokens?.message}>
          <input id="amMaxTokens" type="number" min={1} max={131072} {...modelForm.register("maxTokens", { valueAsNumber: true })} />
        </FormField>
      </div>

      {error && <InlineAlert kind="error" title="添加模型" message={error} />}
    </Drawer>
    </FormProvider>
  );
}

/* ================= MCP 连接抽屉 ================= */

export function McpConnectDrawer({ onClose, onConnected }: { onClose: () => void; onConnected: () => void }) {
  const pasteRef = useRef<HTMLTextAreaElement>(null);
  const mcpForm = useForm<McpFormValues>({
    resolver: zodResolver(mcpSchema) as Resolver<McpFormValues>,
    defaultValues: {
      displayName: "",
      name: "",
      transport: "http",
      command: "",
      args: "",
      endpointUrl: "",
      apiKeyName: "",
      apiKeyValue: "",
      description: "",
    },
  });
  const { transport } = mcpForm.watch();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function parsePaste() {
    const raw = pasteRef.current?.value.trim();
    if (!raw) return;
    let parsed: any;
    try { parsed = JSON.parse(raw); } catch { showToast("配置解析失败", "请粘贴有效的 JSON", "error"); return; }
    const servers = parsed.mcpServers || parsed.mcp_servers || parsed;
    const firstKey = Object.keys(servers)[0];
    const server = servers[firstKey];
    if (!server || typeof server !== "object") { showToast("配置解析失败", "未找到 MCP server 定义", "error"); return; }
    mcpForm.setValue("name", firstKey, { shouldValidate: true });
    mcpForm.setValue("displayName", server.name || firstKey, { shouldValidate: true });
    if (server.description) mcpForm.setValue("description", server.description);
    const type = String(server.type || server.transport || "").toLowerCase();
    if (type.includes("http") || server.url) {
      mcpForm.setValue("transport", type === "sse" ? "sse" : "http");
      if (server.url) mcpForm.setValue("endpointUrl", server.url, { shouldValidate: true });
    } else {
      mcpForm.setValue("transport", "stdio");
      if (server.command) mcpForm.setValue("command", server.command, { shouldValidate: true });
      if (Array.isArray(server.args)) mcpForm.setValue("args", server.args.join(" "));
    }
    const headers = server.headers || {};
    const auth = headers.Authorization || headers.authorization;
    if (auth) {
      const match = String(auth).match(/\$\{?([A-Za-z0-9_]+)\}?/);
      if (match) mcpForm.setValue("apiKeyName", match[1], { shouldValidate: true });
    } else if (server.env_key) {
      mcpForm.setValue("apiKeyName", server.env_key, { shouldValidate: true });
    }
  }

  async function saveAndProbe(values: McpFormValues) {
    setError("");
    setBusy(true);
    try {
      if (values.apiKeyName.trim() && values.apiKeyValue.trim()) {
        const credRes = await apiFetch(`/api/v1/credentials/${encodeURIComponent(values.apiKeyName.trim())}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: values.apiKeyValue.trim(), persistence: "session" }),
        });
        if (!credRes.ok) throw new Error(await errorMessage(credRes, "凭证保存失败"));
      }
      const normalizedTransport = values.transport === "streamable-http" ? "http" : values.transport;
      const envName = normalizedTransport === "stdio" ? values.apiKeyName.trim() : "Authorization";
      const server: any = {
        name: values.name.trim(),
        version: "1.0.0",
        transport: normalizedTransport,
        args: normalizedTransport === "stdio" ? values.args.trim().split(/\s+/).filter(Boolean) : [],
        envRefs: values.apiKeyName.trim() ? { [envName]: `env://${values.apiKeyName.trim()}` } : {},
      };
      if (normalizedTransport === "stdio") server.command = values.command.trim();
      else server.endpointUrl = values.endpointUrl.trim();
      const createRes = await apiFetch("/api/v1/catalog/mcp-servers", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ displayName: values.displayName.trim(), description: values.description.trim(), server }),
      });
      const created = await createRes.json().catch(() => null);
      if (!createRes.ok) {
        if (applyApiFieldErrors(created, mcpForm.setError)) {
          setBusy(false);
          return;
        }
        throw new Error(created?.error?.message || `保存失败（${createRes.status}）`);
      }
      const probeRes = await apiFetch(`/api/v1/catalog/mcp-servers/${encodeURIComponent(created.resourceId)}:probe?timeoutSeconds=15`, { method: "POST" });
      if (!probeRes.ok) throw new Error(await errorMessage(probeRes, "探测失败"));
      const probed = await probeRes.json();
      showToast("MCP 已连接", `已发现 ${probed.health?.toolCount || 0} 个 Tool。`);
      onConnected();
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  }

  return (
    <FormProvider {...mcpForm}>
    <Drawer
      title="连接 MCP Server"
      subtitle="保存后执行探测，再回到 Agent 能力选择。"
      onClose={onClose}
      footer={
        <>
          <button className="button secondary" type="button" onClick={onClose}>取消</button>
          <button className="button accent" type="button" onClick={mcpForm.handleSubmit(saveAndProbe)} disabled={busy}>
            <Network size={15} /><span>{busy ? "正在探测" : "保存并探测"}</span>
          </button>
        </>
      }
    >
      <FormField label="粘贴 MCP 配置" requirement="optional" htmlFor="mcpPaste" hint="粘贴 JSON 后会自动填充下方字段。">
        <textarea
          id="mcpPaste"
          ref={pasteRef}
          rows={5}
          placeholder='{"mcpServers":{"metaso":{"type":"streamable-http","url":"https://...","headers":{"Authorization":"Bearer ${KSC_AIPRO_API_KEY}"}}}}'
          onPaste={() => window.setTimeout(parsePaste, 0)}
        />
      </FormField>
      <div className="form-grid two-columns">
        <FormField label="显示名称" requirement="required" htmlFor="mcpDisplayName" error={mcpForm.formState.errors.displayName?.message}>
          <input id="mcpDisplayName" placeholder="Web Research MCP" {...mcpForm.register("displayName")} />
        </FormField>
        <FormField label="Server 名称" requirement="required" htmlFor="mcpName" error={mcpForm.formState.errors.name?.message}>
          <input id="mcpName" className="mono" placeholder="web-research" {...mcpForm.register("name")} />
        </FormField>
      </div>
      <FormField label="Transport" requirement="required" htmlFor="mcpTransport" error={mcpForm.formState.errors.transport?.message}>
        <StudioSelect
          id="mcpTransport"
          ariaLabel="Transport"
          value={transport}
          options={[
            { value: "http", label: "Streamable HTTP" },
            { value: "stdio", label: "STDIO（本地命令）" },
            { value: "sse", label: "SSE" },
          ]}
          onValueChange={value => mcpForm.setValue("transport", value as McpFormValues["transport"], { shouldDirty: true, shouldValidate: true })}
        />
      </FormField>
      {transport === "stdio" && (
        <>
          <FormField label="Command" requirement="required" htmlFor="mcpCommand" error={mcpForm.formState.errors.command?.message}>
            <input id="mcpCommand" className="mono" placeholder="npx" {...mcpForm.register("command")} />
          </FormField>
          <FormField label="Arguments" requirement="optional" htmlFor="mcpArgs" error={mcpForm.formState.errors.args?.message}>
            <input id="mcpArgs" className="mono" placeholder="-y @your-org/web-research-mcp" {...mcpForm.register("args")} />
          </FormField>
        </>
      )}
      {transport !== "stdio" && (
        <FormField label="Server URL" requirement="required" htmlFor="mcpEndpoint" error={mcpForm.formState.errors.endpointUrl?.message}>
          <input id="mcpEndpoint" className="mono" placeholder="https://mcp.example.com/mcp" {...mcpForm.register("endpointUrl")} />
        </FormField>
      )}
      <FormField label="API Key 环境变量名" requirement="optional" htmlFor="mcpApiKey" error={mcpForm.formState.errors.apiKeyName?.message}>
        <input id="mcpApiKey" className="mono" placeholder="KSC_AIPRO_API_KEY" {...mcpForm.register("apiKeyName")} />
      </FormField>
      <FormField label="API Key 值" requirement="optional" htmlFor="mcpApiKeyValue" hint="仅保存到当前 Studio 会话；留空则从环境变量读取。" error={mcpForm.formState.errors.apiKeyValue?.message}>
        <input id="mcpApiKeyValue" type="password" autoComplete="new-password" placeholder="留空则从环境变量读取" {...mcpForm.register("apiKeyValue")} />
      </FormField>
      <FormField label="说明" requirement="optional" htmlFor="mcpDescription" error={mcpForm.formState.errors.description?.message}>
        <textarea id="mcpDescription" rows={2} placeholder="提供 Web 搜索和页面抓取能力" {...mcpForm.register("description")} />
      </FormField>
      {error && <InlineAlert kind="error" title="连接失败" message={error} />}
    </Drawer>
    </FormProvider>
  );
}

/* ================= MCP 详情抽屉 ================= */

function McpDetailDrawer({ item, onClose }: { item: ResItem; onClose: () => void }) {
  const contract = item.contract || {};
  const health = item.health || {};
  const tools = contract.discoveredTools || health.discoveredTools || [];
  const grid: Array<[string, any]> = [
    ["Resource ID", item.resourceId],
    ["Transport", contract.transport || "-"],
    ["Endpoint", contract.endpointUrl || contract.command || "-"],
    ["状态", health.status || item.status],
    ["工具数", health.toolCount ?? tools.length],
    ["凭证", (item.requiredSecretRefs || []).join("、") || "无"],
  ];
  return (
    <Drawer title={item.displayName || item.name} subtitle={`${item.name} · ${item.version} · ${contract.transport || ""}`} onClose={onClose}>
      <dl className="trace-detail-grid">
        {grid.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>)}
      </dl>
      <div className="inspector-title inspector-title-spaced">已发现工具</div>
      <div className="resource-detail-list">
        {tools.length === 0 ? (
          <div className="resource-detail-item">
            <span className="resource-detail-item-copy"><strong>未发现工具</strong><span>点击“重新探测”以加载工具列表</span></span>
          </div>
        ) : tools.map((t: any) => (
          <div key={t.name} className="resource-detail-item">
            <span className="capability-icon"><Wrench size={15} /></span>
            <span className="resource-detail-item-copy"><strong>{t.name}</strong><span>{t.description || ""}</span></span>
          </div>
        ))}
      </div>
    </Drawer>
  );
}

/* ================= 通用资源详情抽屉（Tool / Skill 查看） ================= */

function ResourceDetailDrawer({ item, onClose }: { item: ResItem; onClose: () => void }) {
  const [skillFilesOpen, setSkillFilesOpen] = useState(false);
  const contract = item.contract || {};
  const rows: Array<[string, any]> = [
    ["Resource ID", item.resourceId],
    ["来源", SOURCE_LABELS[item.source] || item.source],
    ["版本", item.version || "-"],
    ["状态", item.kind === "model" ? (item.status === "ready" ? "凭证已配置" : "凭证未配置") : item.status],
  ];
  if (item.kind === "tool") {
    rows.push(["Tool 分组", contract.group || item.category || "general"]);
    rows.push(["审批", contract.approval === "always" ? "需审批" : "无需审批"]);
    rows.push(["边界", contract.boundary || "ksadk-runtime"]);
    if (contract.sourcePath) rows.push(["源码", `${contract.sourcePath} · ${contract.callableName || "-"}()`]);
  }
  if (item.kind === "skill" && contract?.contentSha256) rows.push(["内容摘要", contract.contentSha256]);
  return (
    <Drawer title={item.displayName || item.name} subtitle={`${item.name} · ${item.version}`} onClose={onClose}>
      <dl className="trace-detail-grid">
        {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>)}
      </dl>
      <div className="inspector-title inspector-title-spaced">说明</div>
      <p style={{ margin: 0, color: "var(--text-secondary)", fontSize: "var(--font-size-meta)", lineHeight: "var(--line-height-body)" }}>
        {item.description || contract.description || "未提供说明"}
      </p>
      {item.kind === "skill" && item.source === "local" && (
        <button className="button secondary skill-files-open" type="button" onClick={() => setSkillFilesOpen(true)}>
          <Eye size={15} /><span>查看 Skill 文件</span>
        </button>
      )}
      {skillFilesOpen && (
        <SkillFileBrowser
          title={item.displayName || item.name}
          endpoint={`/api/v1/catalog/skills/${encodeURIComponent(item.resourceId)}/files`}
          onClose={() => setSkillFilesOpen(false)}
        />
      )}
    </Drawer>
  );
}

/* ================= Skill 发现抽屉 ================= */

interface SkillCandidate extends SkillDiscoveryCandidate {
  fileCount?: number;
  totalBytes?: number;
  risk?: {
    requiresReview?: boolean;
  };
  diagnostics?: Array<{ message?: string }>;
}

function SkillDiscoveryDrawer({
  onClose,
  onCatalogChanged,
}: {
  onClose: () => void;
  onCatalogChanged: () => void | Promise<void>;
}) {
  const [scanPaths, setScanPaths] = useState("");
  const [discovery, setDiscovery] = useState<any>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [results, setResults] = useState<Record<string, SkillImportResult<ResItem>>>({});
  const [summary, setSummary] = useState<SkillImportSummary<ResItem> | null>(null);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const [currentImportId, setCurrentImportId] = useState("");
  const [error, setError] = useState("");
  const [scanning, setScanning] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [conflicts, setConflicts] = useState<SkillCandidate[] | null>(null);
  const [previewCandidate, setPreviewCandidate] = useState<SkillCandidate | null>(null);

  const candidates: SkillCandidate[] = discovery?.candidates || [];

  function resetBatchState() {
    setSelectedIds(new Set());
    setResults({});
    setSummary(null);
    setProgress({ completed: 0, total: 0 });
    setCurrentImportId("");
    setConflicts(null);
  }

  async function scan() {
    setError("");
    setScanning(true);
    try {
      const list = scanPaths.split(",").map(s => s.trim()).filter(Boolean);
      const res = await apiFetch("/api/v1/catalog/skills:discover", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scanPaths: list }),
      });
      if (!res.ok) throw new Error(await errorMessage(res, "Skill 发现失败"));
      const d = await res.json();
      setDiscovery(d);
      resetBatchState();
    } catch (e: any) { setError(e.message); }
    setScanning(false);
  }

  function toggleSelected(candidateId: string) {
    if (committing || results[candidateId]?.status === "succeeded") return;
    setSelectedIds(previous => {
      const next = new Set(previous);
      if (next.has(candidateId)) next.delete(candidateId);
      else next.add(candidateId);
      return next;
    });
  }

  function selectAllEligible() {
    const eligible = eligibleSkillIds(candidates);
    for (const [candidateId, result] of Object.entries(results)) {
      if (result.status === "succeeded") eligible.delete(candidateId);
    }
    setSelectedIds(eligible);
  }

  async function executeBatch(overwriteIds: ReadonlySet<string>) {
    const token = discovery?.inspectionToken;
    if (!selectedIds.size || !token || committing) return;
    const batchSelection = new Set(selectedIds);
    const total = candidates.filter(candidate => (
      batchSelection.has(candidate.candidateId)
      && (candidate.status === "ready" || candidate.status === "conflict")
    )).length;
    setError("");
    setSummary(null);
    setResults({});
    setProgress({ completed: 0, total });
    setCommitting(true);
    try {
      const completed = await runSkillImportBatch<ResItem, SkillCandidate>({
        candidates,
        selectedIds: batchSelection,
        overwriteIds,
        commit: async (candidate, overwrite) => {
          setCurrentImportId(candidate.candidateId);
          const res = await apiFetch(`/api/v1/catalog/skills/discoveries/${encodeURIComponent(token)}:commit`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidateId: candidate.candidateId, overwrite }),
          });
          if (!res.ok) throw new Error(await errorMessage(res, "Skill 导入失败"));
          return res.json();
        },
        onResult: (result, completedCount, batchTotal) => {
          setResults(previous => ({ ...previous, [result.candidateId]: result }));
          setProgress({ completed: completedCount, total: batchTotal });
        },
      });
      setSummary(completed);
      setSelectedIds(new Set(completed.failedIds));
      if (completed.succeededIds.length) {
        await onCatalogChanged();
      }
      if (completed.failedIds.length) {
        showToast(
          "Skill 批量导入完成",
          `已导入 ${completed.succeededIds.length} 个，${completed.failedIds.length} 个失败。`,
          "error",
        );
      } else {
        showToast("Skill 批量导入完成", `已导入 ${completed.succeededIds.length} 个 Skill。`);
      }
    } catch (e: any) { setError(e.message); }
    setCurrentImportId("");
    setCommitting(false);
  }

  function beginImport() {
    const selectedConflicts = candidates.filter(candidate => (
      selectedIds.has(candidate.candidateId) && candidate.status === "conflict"
    ));
    if (selectedConflicts.length) {
      setConflicts(selectedConflicts);
      return;
    }
    void executeBatch(new Set());
  }

  const selectedCount = selectedIds.size;

  return (
    <Drawer
      title="发现本地 Skill"
      subtitle="默认扫描工作区及允许的 Claude、Codex、Agent 用户目录；扫描只产生候选，确认后才导入。"
      wide
      closeDisabled={committing}
      onClose={onClose}
      footer={
        <>
          <button className="button secondary" type="button" onClick={onClose} disabled={committing}>取消</button>
          <button className="button accent" type="button" onClick={beginImport} disabled={!selectedCount || committing}>
            <Check size={15} />
            <span>{committing ? `正在导入 ${progress.completed}/${progress.total}` : `导入所选 ${selectedCount} 个`}</span>
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor="skillScanPaths">扫描目录（逗号分隔；留空扫描安全默认目录）</label>
        <input id="skillScanPaths" value={scanPaths} onChange={e => setScanPaths(e.target.value)} placeholder="skills, .claude/skills, user:codex" />
        <span className="helper">用户目录仅支持 user:agents、user:codex、user:claude；不支持任意本机路径扫描。</span>
      </div>
      <button className="button secondary" type="button" onClick={scan} disabled={scanning || committing}>
        <Search size={15} /><span>{scanning ? "正在扫描" : "扫描候选"}</span>
      </button>
      {candidates.length > 0 && (
        <div className="skill-selection-toolbar">
          <span>已选择 {selectedCount} / {eligibleSkillIds(candidates).size}</span>
          <span className="skill-selection-actions">
            <button className="button tertiary small" type="button" onClick={selectAllEligible} disabled={committing}>全选可导入</button>
            <button className="button tertiary small" type="button" onClick={() => setSelectedIds(new Set())} disabled={!selectedCount || committing}>清空选择</button>
          </span>
        </div>
      )}
      <div className="skill-discovery-list" style={{ marginTop: 16 }}>
        {candidates.length === 0 ? (
          <div className="trace-stage-empty compact">
            <p>{discovery ? "安全默认目录中没有发现 Skill。" : "点击扫描候选。"}</p>
          </div>
        ) : candidates.map(candidate => {
          const risk = candidate.risk || {};
          const valid = ["ready", "conflict"].includes(candidate.status);
          const result = results[candidate.candidateId];
          const alreadyImported = result?.status === "succeeded";
          const statusLabel = candidate.status === "ready"
            ? "可导入"
            : candidate.status === "conflict"
              ? "已安装"
              : "无效";
          const details = candidate.diagnostics?.map((d: any) => d.message).join("；")
            || `${candidate.fileCount || 0} 个文件 · ${formatByteCount(candidate.totalBytes || 0)}`;
          const importState = result?.status === "succeeded"
            ? "已导入"
            : result?.status === "failed"
              ? `导入失败：${result.error || "未知错误"}`
              : currentImportId === candidate.candidateId
                ? "正在导入"
                : committing && selectedIds.has(candidate.candidateId)
                  ? "等待导入"
                  : "";
          return (
            <div key={candidate.candidateId} className={`skill-candidate${valid ? "" : " invalid"}`}>
              <input
                type="checkbox"
                aria-label={`选择 ${candidate.displayName || candidate.name}`}
                disabled={!valid || committing || alreadyImported}
                checked={selectedIds.has(candidate.candidateId)}
                onChange={() => toggleSelected(candidate.candidateId)}
              />
              <span className="capability-icon"><Sparkles size={15} /></span>
              <span className="skill-candidate-copy">
                <strong>{candidate.displayName || candidate.name}</strong>
                <span>{candidate.path} · {candidate.version || "版本无效"}</span>
                <small>{details}</small>
                {importState && <small className={`skill-import-state ${result?.status || "pending"}`}>{importState}</small>}
              </span>
              <span className="badge" data-state={candidate.status === "ready" ? "ready" : "pending"}>
                {statusLabel}{risk.requiresReview ? " · 需复核" : ""}
              </span>
              {valid && (
                <button className="button tertiary small skill-preview-button" type="button" onClick={() => setPreviewCandidate(candidate)}>
                  <Eye size={14} /><span>查看详情</span>
                </button>
              )}
            </div>
          );
        })}
      </div>
      {summary && (
        <InlineAlert
          kind={summary.failedIds.length ? "warning" : "success"}
          title={summary.failedIds.length
            ? `已导入 ${summary.succeededIds.length} 个，${summary.failedIds.length} 个失败`
            : `已导入 ${summary.succeededIds.length} 个 Skill`}
          message={summary.failedIds.length ? "失败项已保留选择，可修复后再次导入。" : "资源目录已刷新。"}
        />
      )}
      {error && <InlineAlert kind="error" title="Skill 发现或导入失败" message={error} />}
      {conflicts && (
        <ConfirmDialog
          title={`所选 Skill 中有 ${conflicts.length} 个已安装`}
          description={`${conflicts.map(candidate => candidate.displayName || candidate.name).join("、")} 将被覆盖；旧版本会移入回收位置，可手工恢复。`}
          confirmText="覆盖并继续"
          danger={false}
          onCancel={() => setConflicts(null)}
          onConfirm={() => {
            const overwriteIds = new Set(conflicts.map(candidate => candidate.candidateId));
            setConflicts(null);
            void executeBatch(overwriteIds);
          }}
        />
      )}
      {previewCandidate && discovery?.inspectionToken && (
        <SkillFileBrowser
          title={previewCandidate.displayName || previewCandidate.name || "Skill"}
          endpoint={`/api/v1/catalog/skills/discoveries/${encodeURIComponent(discovery.inspectionToken)}/candidates/${encodeURIComponent(previewCandidate.candidateId)}/files`}
          onClose={() => setPreviewCandidate(null)}
        />
      )}
    </Drawer>
  );
}

/* ================= Python Tool 抽屉 ================= */

function PythonToolDrawer({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const pythonForm = useForm<PythonToolFormValues>({
    resolver: zodResolver(pythonToolSchema) as Resolver<PythonToolFormValues>,
    defaultValues: {
      displayName: "",
      name: "",
      callableName: "",
      description: "",
      sourceMode: "upload",
      sourcePath: "",
    },
  });
  const { sourceMode, name, callableName } = pythonForm.watch();
  const [file, setFile] = useState<File | null>(null);
  const [inspection, setInspection] = useState<any>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function inspectUpload() {
    if (!file) { setError("请先选择 Python 文件"); return; }
    setSaving(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await apiFetch("/api/v1/catalog/python-tools:inspect", { method: "POST", body });
      if (!response.ok) throw new Error(await errorMessage(response, "Python Tool 检查失败"));
      const payload = await response.json();
      const first = payload.callables?.[0];
      setInspection(payload);
      const generatedName = file.name.replace(/\.py$/i, "").replace(/[^A-Za-z0-9_]+/g, "_").replace(/^[^A-Za-z_]+/, "tool_") || "python_tool";
      pythonForm.setValue("callableName", first?.name || "", { shouldValidate: true });
      if (!pythonForm.getValues("name")) pythonForm.setValue("name", generatedName, { shouldValidate: true });
      if (!pythonForm.getValues("displayName")) pythonForm.setValue("displayName", generatedName, { shouldValidate: true });
      if (!pythonForm.getValues("description")) pythonForm.setValue("description", first?.description || "");
    } catch (reason: any) {
      setInspection(null);
      setError(reason.message || "Python Tool 检查失败");
    } finally {
      setSaving(false);
    }
  }

  async function save(values: PythonToolFormValues) {
    if (values.sourceMode === "upload" && !inspection) {
      pythonForm.setError("callableName", { type: "manual", message: "请先完成 Python 文件检查" });
      return;
    }
    setSaving(true); setError("");
    try {
      const res = values.sourceMode === "upload"
        ? await apiFetch(`/api/v1/catalog/python-tools/${encodeURIComponent(inspection.inspectionToken)}:commit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              displayName: values.displayName.trim(),
              name: values.name.trim(),
              callableName: values.callableName.trim(),
              description: values.description.trim(),
            }),
          })
        : await apiFetch("/api/v1/catalog/tools", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              displayName: values.displayName.trim(),
              category: "custom",
              contract: {
                name: values.name.trim(),
                version: "1.0.0",
                description: values.description.trim(),
                inputSchema: { type: "object", properties: {} },
                outputSchema: {},
                approval: "policy",
                sideEffect: "none",
                executor: "python",
                sourcePath: values.sourcePath.trim(),
                callableName: values.callableName.trim(),
              },
            }),
          });
      const created = await res.json().catch(() => null);
      if (!res.ok) {
        if (applyApiFieldErrors(created, pythonForm.setError)) {
          setSaving(false);
          return;
        }
        throw new Error(created?.error?.message || `Tool 添加失败（${res.status}）`);
      }
      showToast("Python Tool 已保存", `${created.displayName || values.displayName.trim()} · SHA-256 已锁定`);
      onAdded();
    } catch (e: any) { setError(e.message); }
    setSaving(false);
  }

  return (
    <FormProvider {...pythonForm}>
    <Drawer
      title="添加 Python Tool"
      subtitle="源码先复制进 Catalog 并锁定 SHA-256，构建时进入不可变 Runtime 快照。"
      onClose={onClose}
      footer={
        <>
          <button className="button secondary" type="button" onClick={onClose}>取消</button>
          <button className="button accent" type="button" onClick={pythonForm.handleSubmit(save)} disabled={saving}>
            <Check size={15} /><span>{saving ? "正在保存" : "保存 Tool"}</span>
          </button>
        </>
      }
    >
      <div className="segmented-control python-tool-source-mode" aria-label="Python Tool 源码方式">
        <button className={sourceMode === "upload" ? "selected" : ""} type="button" onClick={() => { pythonForm.setValue("sourceMode", "upload", { shouldValidate: true }); setError(""); }}>上传文件</button>
        <button className={sourceMode === "workspace" ? "selected" : ""} type="button" onClick={() => { pythonForm.setValue("sourceMode", "workspace", { shouldValidate: true }); setError(""); }}>工作区路径</button>
      </div>
      <PythonToolExample />
      {sourceMode === "upload" ? (
        <FormField label="Python 文件" requirement="required" hint="拖放 .py 文件，或点击选择；检查只解析 AST，不会执行脚本。">
          <div>
          <FileDropzone
            ariaLabel="选择 Python Tool 文件"
            accept={{ "text/x-python": [".py"], "text/plain": [".py"] }}
            maxSize={1024 * 1024}
            file={file}
            onFile={next => { setFile(next); setInspection(null); pythonForm.setValue("callableName", ""); }}
            onError={setError}
          />
          <button className="button secondary" type="button" onClick={inspectUpload} disabled={!file || saving}>
            <Search size={15} /><span>{saving ? "检查中" : "只读检查 Callable"}</span>
          </button>
          {inspection && (
            <div className="python-tool-inspection-summary">
              <Check size={15} /><span>SHA-256 {inspection.sha256.slice(0, 12)}… · 发现 {inspection.callables.length} 个公开函数</span>
            </div>
          )}
          </div>
        </FormField>
      ) : (
        <FormField label="工作区 Python 文件" requirement="required" htmlFor="ptSource" hint="仅允许当前工作区内的普通 .py 文件；符号链接会被拒绝。" error={pythonForm.formState.errors.sourcePath?.message}>
          <input id="ptSource" placeholder="tools/my_tool.py" {...pythonForm.register("sourcePath")} />
        </FormField>
      )}
      <div className="form-grid two-columns">
        <FormField label="显示名称" requirement="required" htmlFor="ptDisplayName" error={pythonForm.formState.errors.displayName?.message}>
          <input id="ptDisplayName" placeholder="例如：订单查询" {...pythonForm.register("displayName")} />
        </FormField>
        <FormField label="Tool 标识" requirement="required" htmlFor="ptName" error={pythonForm.formState.errors.name?.message}>
          <input id="ptName" className="mono" {...pythonForm.register("name")} />
        </FormField>
        <FormField label="Callable" requirement="required" htmlFor="ptCallable" error={pythonForm.formState.errors.callableName?.message}>
          {sourceMode === "upload" ? (
            <StudioSelect
              id="ptCallable"
              ariaLabel="Callable"
              value={callableName}
              placeholder={inspection ? "选择公开函数" : "请先检查文件"}
              disabled={!inspection}
              options={(inspection?.callables || []).map((item: any) => ({
                value: item.name,
                label: `${item.async ? "async " : ""}${item.name}(${item.parameters.join(", ")})`,
                description: item.description || undefined,
              }))}
              onValueChange={value => {
              pythonForm.setValue("callableName", value, { shouldDirty: true, shouldValidate: true });
              const selected = inspection?.callables?.find((item: any) => item.name === value);
              if (selected?.description) pythonForm.setValue("description", selected.description);
              }}
            />
          ) : (
            <input id="ptCallable" className="mono" {...pythonForm.register("callableName")} />
          )}
        </FormField>
      </div>
      <FormField label="说明" requirement="optional" htmlFor="ptDesc" error={pythonForm.formState.errors.description?.message}>
        <textarea id="ptDesc" rows={3} {...pythonForm.register("description")} />
      </FormField>
      {error && <InlineAlert kind="error" title="Tool 添加失败" message={error} />}
    </Drawer>
    </FormProvider>
  );
}
