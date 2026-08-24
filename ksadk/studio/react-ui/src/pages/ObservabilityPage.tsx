import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity, Archive, ArrowLeft, Brain, Check, CircleCheckBig, Clock3, Coins, Copy, MessageSquare,
  ChevronDown, ChevronUp, Maximize2, Minimize2, Network, Search,
} from "lucide-react";
import { allExpanded, collapseAllNested, JsonView } from "react-json-view-lite";
import { showToast } from "../components/Toast";
import { AgentAvatar, type AgentAppearance } from "../components/AgentAvatar";
import {
  StudioDataTable,
  type StudioDataColumn,
} from "../components/ui/StudioDataTable";
import { StudioSelect } from "../components/ui/StudioSelect";
import { PageHeaderActions, PageHeaderTools } from "../components/PageHeaderPortal";
import { apiFetch } from "../api";
import { TrajectoryDetail, TrajectoryView } from "./TrajectoryView";
import type { TrajectoryRecord } from "./trajectory";

/* ================= 类型 ================= */

interface TraceSummary {
  traceId: string; runId: string; agentId: string; sessionId: string;
  runtimeType: string; model: string; status: string; startedAt: string;
  durationMs: number | null; totalTokens: number | null; usageReported?: boolean;
  inputTokens?: number | null; outputTokens?: number | null;
  usageCompleteness?: TokenUsageCompleteness;
  spanCount: number;
}
interface SpanEvent { name: string; timeUnixNano: string; attributes?: Record<string, any> }
interface Span {
  spanId: string; parentSpanId?: string; name: string; kind: string; status: string;
  startTimeUnixNano: string; endTimeUnixNano: string; durationMs: number;
  attributes?: Record<string, any>; events?: SpanEvent[];
}
interface TraceDetail extends TraceSummary {
  rootSpanId: string;
  metrics?: {
    durationMs?: number | null; durationSource?: string | null;
    inputTokens?: number | null; outputTokens?: number | null; totalTokens?: number | null;
    usageReported?: boolean; usageSource?: string | null;
    usageCompleteness?: TokenUsageCompleteness;
  };
  target?: { name?: string };
  resource?: Record<string, any>;
  scope?: { name?: string; version?: string };
  spans: Span[];
}
interface TraceOverview {
  range: "24h" | "7d";
  total: number;
  completed: number;
  successRate: number | null;
  averageDurationMs: number | null;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  buckets: Array<{ startedAt: string; runs: number; completed: number }>;
}

/* ================= Trace 数据格式化 ================= */

function shortId(value: any, length = 18): string {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length)}…` : text;
}
function formatField(value: any, fallback = "-"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}
function statusLabel(value: any): string {
  const status = String(value || "").toUpperCase();
  if (status === "COMPLETED" || status === "SUCCEEDED" || status === "OK") return "成功";
  if (status === "RUNNING") return "运行中";
  if (status === "PAUSED") return "已暂停";
  if (status === "FAILED" || status === "ERROR" || status === "INTERNAL") return "失败";
  if (status === "CANCELLED") return "已取消";
  return status || "未知";
}
function formatDuration(value: any): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "未上报";
  const ms = Number(value);
  if (ms < 1) return `${ms.toFixed(3)} ms`;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}
function formatTokenCount(value: any): string {
  if (value === null || value === undefined) return "未上报";
  return new Intl.NumberFormat("zh-CN").format(Number(value));
}
type TokenUsage = {
  inputTokens?: number | null;
  outputTokens?: number | null;
  totalTokens?: number | null;
  usageReported?: boolean;
  usageCompleteness?: TokenUsageCompleteness;
};
type TokenUsageCompleteness = {
  inputTokens?: boolean;
  outputTokens?: boolean;
  totalTokens?: boolean;
  cachedInputTokens?: boolean;
  reasoningOutputTokens?: boolean;
};
function tokenCounter(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean") return null;
  const counter = Number(value);
  return Number.isFinite(counter) && counter >= 0 ? counter : null;
}
function normalizedTokenTotal(usage: TokenUsage): number | null {
  const total = tokenCounter(usage.totalTokens);
  if (total !== null && usage.usageCompleteness?.totalTokens !== false) return total;
  const input = tokenCounter(usage.inputTokens);
  const output = tokenCounter(usage.outputTokens);
  return input !== null
    && output !== null
    && usage.usageCompleteness?.inputTokens !== false
    && usage.usageCompleteness?.outputTokens !== false
    ? input + output
    : null;
}
function hasTokenUsage(usage: TokenUsage): boolean {
  return [usage.inputTokens, usage.outputTokens, usage.totalTokens]
    .some(value => tokenCounter(value) !== null);
}
function tokenUsageHeadline(usage: TokenUsage): string {
  const total = normalizedTokenTotal(usage);
  if (total !== null) return formatTokenCount(total);
  return hasTokenUsage(usage) ? "部分上报" : "未上报";
}
function tokenUsageBreakdown(usage: TokenUsage): string {
  const input = tokenCounter(usage.inputTokens);
  const output = tokenCounter(usage.outputTokens);
  const inputLabel = input === null
    ? "—"
    : `${usage.usageCompleteness?.inputTokens === false ? "≥" : ""}${formatTokenCount(input)}`;
  const outputLabel = output === null
    ? "—"
    : `${usage.usageCompleteness?.outputTokens === false ? "≥" : ""}${formatTokenCount(output)}`;
  return `${inputLabel} 输入 · ${outputLabel} 输出`;
}
function tokenUsageListLabel(usage: TokenUsage): string {
  const total = normalizedTokenTotal(usage);
  if (total !== null) return formatTokenCount(total);
  if (tokenCounter(usage.totalTokens) !== null) return "部分上报";
  const input = tokenCounter(usage.inputTokens);
  const output = tokenCounter(usage.outputTokens);
  if (input !== null && output !== null) return "部分上报";
  if (input !== null && usage.usageCompleteness?.inputTokens === false) return "部分上报";
  if (output !== null && usage.usageCompleteness?.outputTokens === false) return "部分上报";
  if (input !== null) return `${formatTokenCount(input)} 输入`;
  if (output !== null) return `${formatTokenCount(output)} 输出`;
  return "未上报";
}
function formatNanoseconds(value: any): string {
  try {
    const ms = Number(BigInt(String(value || "0")) / 1000000n);
    return ms ? new Date(ms).toISOString() : "-";
  } catch { return "-"; }
}
function formatDate(value: any): string {
  if (!value) return "刚刚";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}
function safeJson(v: any): string {
  try {
    return JSON.stringify(v, (_k, val) => (typeof val === "bigint" ? val.toString() : val), 2);
  } catch { return String(v); }
}
function fmtVal(v: any): string {
  if (v === null || v === undefined) return "-";
  if (typeof v === "object") return safeJson(v);
  return String(v);
}

const OTLP_JSON_STYLES = {
  container: "otlp-json",
  childFieldsContainer: "otlp-json-children",
  basicChildStyle: "otlp-json-row",
  collapseIcon: "otlp-json-toggle otlp-json-collapse",
  expandIcon: "otlp-json-toggle otlp-json-expand",
  collapsedContent: "otlp-json-collapsed",
  label: "otlp-json-key",
  clickableLabel: "otlp-json-key otlp-json-key-clickable",
  nullValue: "otlp-json-null",
  undefinedValue: "otlp-json-null",
  numberValue: "otlp-json-number",
  stringValue: "otlp-json-string",
  booleanValue: "otlp-json-boolean",
  otherValue: "otlp-json-other",
  punctuation: "otlp-json-punctuation",
  quotesForFieldNames: true,
  stringifyStringValues: true,
  ariaLables: {
    collapseJson: "收起 JSON 节点",
    expandJson: "展开 JSON 节点",
  },
};

/* ================= 内容聚合（模型输出 / 思考 / 工具 I/O） ================= */

const CONTENT_ATTR_KEYS = [
  "agentkit.event.text", "agentkit.event.delta", "agentkit.event.content", "agentkit.event.output",
  "agentkit.event.args", "agentkit.event.command", "agentkit.event.input", "agentkit.event.prompt",
];
function eventContent(attrs?: Record<string, any>): string {
  if (!attrs) return "";
  for (const key of CONTENT_ATTR_KEYS) {
    const v = attrs[key];
    if (typeof v === "string" && v.trim()) return v;
    if (typeof v === "number" || typeof v === "boolean") return String(v);
  }
  return "";
}

interface ContentCard { kind: "thinking" | "message"; text: string; segments: number; final: boolean }

function aggregateContent(events: SpanEvent[]): ContentCard[] {
  const cards: ContentCard[] = [];
  let buf = "";
  let kind: "thinking" | "message" | null = null;
  let segments = 0;
  const flush = (final = false) => {
    if (kind && buf.trim()) cards.push({ kind, text: buf, segments, final });
    buf = ""; segments = 0;
  };
  const finalTexts: Partial<Record<"thinking" | "message", string>> = {};
  for (const e of events || []) {
    const text = eventContent(e.attributes);
    if (e.name === "thinking.delta" || e.name === "message.delta") {
      const k = e.name === "thinking.delta" ? "thinking" : "message";
      if (kind !== k) { flush(); kind = k; }
      buf += text;
      segments += 1;
    } else if (e.name === "thinking.completed" || e.name === "message.completed") {
      const k = e.name === "thinking.completed" ? "thinking" : "message";
      if (text) finalTexts[k] = text;
    }
  }
  flush();
  for (const k of ["thinking", "message"] as const) {
    const finalText = finalTexts[k];
    if (!finalText) continue;
    const idx = cards.findIndex(c => c.kind === k);
    if (idx >= 0) cards[idx] = { kind: k, text: finalText, segments: cards[idx].segments, final: true };
    else cards.push({ kind: k, text: finalText, segments: 1, final: true });
  }
  return cards;
}

/* ================= 复制按钮 ================= */

async function writeClipboard(text: string) {
  try { await navigator.clipboard.writeText(text); return; } catch { /* fallback */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* noop */ }
  ta.remove();
}

function CopyButton({ text, className }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={`copy-btn ${className || ""}`}
      title="复制"
      onClick={e => {
        e.stopPropagation();
        writeClipboard(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <Check size={12} style={{ color: "var(--success)" }} /> : <Copy size={12} />}
    </button>
  );
}

/* ================= 内容卡片 ================= */

function IoBlock({ label, text, tone, icon, meta }: {
  label: string; text: string; tone: "message" | "thinking" | "tool"; icon?: ReactNode; meta?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > 600;
  const shown = expanded || !long ? text : `${text.slice(0, 600)}…`;
  return (
    <div className={`io-block io-${tone}`}>
      <div className="io-head">
        <span className="io-label">{icon}{label}</span>
        {meta && <span className="io-meta">{meta}</span>}
        <span style={{ flex: 1 }} />
        <CopyButton text={text} className="copy-visible" />
      </div>
      <div className="io-text">{shown}</div>
      {long && (
        <button type="button" className="io-expand" onClick={() => setExpanded(!expanded)}>
          {expanded ? "收起" : `展开全文（${text.length} 字符）`}
        </button>
      )}
    </div>
  );
}

function SpanContentCards({ span }: { span: Span }) {
  const attrs = span.attributes || {};
  const toolInput = attrs["agentkit.tool.input"];
  const toolOutput = attrs["agentkit.tool.output"];
  const cards = aggregateContent(span.events || []);
  const hasContent = (toolInput !== undefined && toolInput !== null && toolInput !== "")
    || (toolOutput !== undefined && toolOutput !== null && toolOutput !== "")
    || cards.length > 0;
  if (!hasContent) {
    return <div className="trace-stage-empty compact"><p>该 Span 没有捕获内容（可在 设置 → 可观测 开启 Trace 内容记录）。</p></div>;
  }
  return (
    <div className="io-stack">
      {toolInput !== undefined && toolInput !== null && toolInput !== "" && (
        <IoBlock label="工具输入" text={fmtVal(toolInput)} tone="tool" />
      )}
      {toolOutput !== undefined && toolOutput !== null && toolOutput !== "" && (
        <IoBlock label="工具输出" text={fmtVal(toolOutput)} tone="tool" />
      )}
      {cards.map((c, i) => (
        <IoBlock
          key={i}
          label={c.kind === "message" ? "模型输出" : "思考过程"}
          text={c.text}
          tone={c.kind}
          icon={c.kind === "message" ? <MessageSquare size={12} /> : <Brain size={12} />}
          meta={c.final ? "完整" : `${c.segments} 段增量`}
        />
      ))}
    </div>
  );
}

/* ================= KV 列表 ================= */

function KvList({ values }: { values: Record<string, any> }) {
  const entries = Object.entries(values || {}).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return <div className="trace-stage-empty compact"><p>没有可展示的字段。</p></div>;
  return (
    <dl className="trace-kv-list">
      {entries.map(([key, value]) => (
        <div key={key} className="trace-kv-row">
          <dt className="trace-kv-key">{key}</dt>
          <dd className="trace-kv-value">{fmtVal(value)}</dd>
          <CopyButton text={fmtVal(value)} className="trace-kv-copy" />
        </div>
      ))}
    </dl>
  );
}

/* ================= 主页面 ================= */

type DetailTab = "spans" | "trajectory" | "attributes" | "events" | "resource" | "raw";
const DETAIL_TABS: Array<{ id: DetailTab; label: string }> = [
  { id: "spans", label: "Spans" },
  { id: "trajectory", label: "轨迹" },
  { id: "attributes", label: "Attributes" },
  { id: "events", label: "Events" },
  { id: "resource", label: "Resource" },
  { id: "raw", label: "Raw OTLP" },
];

const TRACE_PAGE_SIZE = 50;

export function ObservabilityPage({ refreshTick }: { refreshTick: number }) {
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [agents, setAgents] = useState<Array<{ id: string; name: string; appearance?: AgentAppearance }>>([]);
  const [search, setSearch] = useState("");
  const [agentFilter, setAgentFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [range, setRange] = useState<"24h" | "7d">("24h");
  const [overview, setOverview] = useState<TraceOverview | null>(null);
  const [activeTrace, setActiveTrace] = useState<TraceDetail | null>(null);
  const [activeSpanId, setActiveSpanId] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>("spans");
  const [selectedTrajectory, setSelectedTrajectory] = useState<TrajectoryRecord | null>(null);
  const [rawOtlp, setRawOtlp] = useState<any>(null);
  const [rawLoading, setRawLoading] = useState(false);
  const [rawExpanded, setRawExpanded] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [detailCollapsed, setDetailCollapsed] = useState(false);
  const [listPage, setListPage] = useState(0);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([null]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [traceTotal, setTraceTotal] = useState(0);
  const [traceLoading, setTraceLoading] = useState(true);
  const [traceError, setTraceError] = useState("");
  const requestSeq = useRef(0);
  const listRequestSeq = useRef(0);

  useEffect(() => {
    apiFetch("/api/v1/agents?limit=100").then(r => r.json()).then(d => {
      setAgents((d.items || []).map((a: any) => ({ id: a.metadata.id, name: a.metadata.name, appearance: a.metadata.appearance })));
    }).catch(() => {});
  }, [refreshTick]);

  const openTrace = useCallback(async (traceId: string) => {
    const seq = ++requestSeq.current;
    try {
      const trace: TraceDetail = await apiFetch(`/api/v1/traces/${encodeURIComponent(traceId)}`).then(r => r.json());
      if (requestSeq.current !== seq) return;
      setActiveTrace(trace);
      setActiveSpanId(trace.rootSpanId || trace.spans?.[0]?.spanId || null);
      setTab("spans");
      setSelectedTrajectory(null);
      setRawOtlp(null);
      setRawExpanded(true);
      setExpanded(false);
      setDetailCollapsed(false);
    } catch { /* 保持当前选择 */ }
  }, []);

  const loadTracePage = useCallback(async (cursor: string | null) => {
    const seq = ++listRequestSeq.current;
    const query = new URLSearchParams({ limit: String(TRACE_PAGE_SIZE), sort: "startedAt:desc" });
    if (agentFilter) query.set("agentId", agentFilter);
    if (statusFilter) query.set("status", statusFilter);
    if (search.trim()) query.set("query", search.trim());
    if (cursor) query.set("cursor", cursor);
    setTraceLoading(true);
    setTraceError("");
    try {
      const response = await apiFetch(`/api/v1/traces?${query}`);
      if (!response.ok) throw new Error(`Trace 列表加载失败（${response.status}）`);
      const payload = await response.json();
      if (listRequestSeq.current !== seq) return;
      const items: TraceSummary[] = payload.items || [];
      setTraces(items);
      setNextCursor(payload.nextCursor || null);
      setTraceTotal(Number(payload.total) || 0);
    } catch (error) {
      if (listRequestSeq.current !== seq) return;
      setTraces([]);
      setNextCursor(null);
      setTraceTotal(0);
      setTraceError(error instanceof Error ? error.message : "Trace 列表加载失败");
    } finally {
      if (listRequestSeq.current === seq) setTraceLoading(false);
    }
  }, [agentFilter, search, statusFilter]);

  useEffect(() => {
    setCursorStack([null]);
    setListPage(0);
    void loadTracePage(null);
  }, [loadTracePage, refreshTick]);

  useEffect(() => {
    const query = new URLSearchParams({ range });
    if (agentFilter) query.set("agentId", agentFilter);
    if (statusFilter) query.set("status", statusFilter);
    apiFetch(`/api/v1/traces/overview?${query}`)
      .then(response => response.ok ? response.json() : Promise.reject(new Error()))
      .then(setOverview)
      .catch(() => setOverview(null));
  }, [agentFilter, range, refreshTick, statusFilter]);

  // raw OTLP 懒加载
  useEffect(() => {
    if (tab !== "raw" || !activeTrace || rawOtlp) return;
    const traceId = activeTrace.traceId;
    setRawLoading(true);
    apiFetch(`/api/v1/traces/${encodeURIComponent(traceId)}/otlp`)
      .then(r => r.json())
      .then(d => { setRawOtlp(d); setRawLoading(false); })
      .catch(() => setRawLoading(false));
  }, [tab, activeTrace, rawOtlp]);

  const activeSpan = activeTrace?.spans?.find(s => s.spanId === activeSpanId) || null;

  const orderedSpans = useMemo(() => {
    const spans = activeTrace?.spans || [];
    const byParent = new Map<string, Span[]>();
    spans.forEach(span => {
      const parent = span.parentSpanId || "";
      if (!byParent.has(parent)) byParent.set(parent, []);
      byParent.get(parent)!.push(span);
    });
    byParent.forEach(children => children.sort((a, b) => {
      try { return Number(BigInt(a.startTimeUnixNano || "0") - BigInt(b.startTimeUnixNano || "0")); } catch { return 0; }
    }));
    const ordered: Array<{ span: Span; depth: number }> = [];
    const visited = new Set<string>();
    const visit = (span: Span | undefined, depth: number) => {
      if (!span || visited.has(span.spanId)) return;
      visited.add(span.spanId);
      ordered.push({ span, depth });
      (byParent.get(span.spanId) || []).forEach(child => visit(child, depth + 1));
    };
    const root = spans.find(s => s.spanId === activeTrace?.rootSpanId) || spans.find(s => !s.parentSpanId);
    visit(root, 0);
    spans.forEach(s => visit(s, s.parentSpanId ? 1 : 0));
    return ordered;
  }, [activeTrace]);

  const rootSpan = orderedSpans.find(i => i.span.spanId === activeTrace?.rootSpanId)?.span || orderedSpans[0]?.span;
  const rootRange = useMemo(() => {
    if (!rootSpan) return { start: 0n, duration: 1 };
    try {
      const start = BigInt(rootSpan.startTimeUnixNano || "0");
      const end = BigInt(rootSpan.endTimeUnixNano || rootSpan.startTimeUnixNano || "0");
      return { start, duration: Number(end > start ? end - start : 1n) };
    } catch { return { start: 0n, duration: 1 }; }
  }, [rootSpan]);

  /* ---------- 渲染 ---------- */

  const metrics: NonNullable<TraceDetail["metrics"]> = activeTrace?.metrics || {
    durationMs: activeTrace?.durationMs,
    inputTokens: activeTrace?.inputTokens,
    outputTokens: activeTrace?.outputTokens,
    totalTokens: activeTrace?.totalTokens,
    usageReported: activeTrace?.usageReported,
  };
  const activeTraceAgent = agents.find(agent => agent.id === activeTrace?.agentId);
  const traceColumns = useMemo<StudioDataColumn<TraceSummary>[]>(() => [
    {
      id: "status",
      header: "状态",
      width: 130,
      cell: trace => <span className={`trace-table-status ${trace.status}`} title={trace.status}><span className={`trace-list-status ${trace.status}`} />{statusLabel(trace.status)}</span>,
    },
    {
      id: "identity",
      header: "Agent / Run",
      minWidth: 270,
      cell: trace => {
        const agent = agents.find(item => item.id === trace.agentId);
        const name = agent?.name || trace.agentId || "unknown-agent";
        return (
          <button type="button" className="trace-table-open trace-table-agent" onClick={() => openTrace(trace.traceId)}>
            <AgentAvatar name={name} appearance={agent?.appearance} size="xs" />
            <span><strong>{name}</strong><small>{shortId(trace.runId || trace.traceId, 32)}</small></span>
          </button>
        );
      },
    },
    { id: "startedAt", header: "开始时间", minWidth: 140, cell: trace => formatDate(trace.startedAt) },
    { id: "duration", header: "耗时", width: 110, cell: trace => formatDuration(trace.durationMs) },
    { id: "model", header: "模型", minWidth: 140, cell: trace => trace.model || "-" },
    { id: "tokens", header: "Token", width: 110, cell: trace => tokenUsageListLabel(trace) },
    { id: "spans", header: "Span", width: 80, cell: trace => trace.spanCount || 0 },
    {
      id: "actions",
      header: <span className="sr-only">操作</span>,
      width: 110,
      className: "actions-column",
      headerClassName: "actions-column",
      cell: trace => <button type="button" className="button tertiary small" onClick={() => openTrace(trace.traceId)}>查看详情</button>,
    },
  ], [agents, openTrace]);

  const refreshTraces = useCallback(() => {
    if (activeTrace?.traceId) {
      void openTrace(activeTrace.traceId);
      return;
    }
    void loadTracePage(cursorStack[listPage] || null);
  }, [activeTrace?.traceId, cursorStack, listPage, loadTracePage, openTrace]);

  const previousTracePage = useCallback(() => {
    if (listPage <= 0) return;
    const page = listPage - 1;
    setListPage(page);
    void loadTracePage(cursorStack[page] || null);
  }, [cursorStack, listPage, loadTracePage]);

  const nextTracePage = useCallback(() => {
    if (!nextCursor) return;
    const page = listPage + 1;
    setCursorStack(current => {
      const next = current.slice(0, page);
      next[page] = nextCursor;
      return next;
    });
    setListPage(page);
    void loadTracePage(nextCursor);
  }, [listPage, loadTracePage, nextCursor]);

  async function copyTraceparent() {
    if (!activeTrace?.traceId || !activeTrace.rootSpanId) return;
    await writeClipboard(`00-${activeTrace.traceId}-${activeTrace.rootSpanId}-01`);
    showToast("traceparent 已复制", shortId(activeTrace.traceId, 24));
  }
  async function copyRawOtlp() {
    if (!activeTrace) return;
    let raw = rawOtlp;
    if (!raw) {
      raw = await apiFetch(`/api/v1/traces/${encodeURIComponent(activeTrace.traceId)}/otlp`).then(r => r.json()).catch(() => null);
      if (raw) setRawOtlp(raw);
    }
    if (!raw) return;
    await writeClipboard(JSON.stringify(raw, null, 2));
    showToast("Raw OTLP 已复制", shortId(activeTrace.traceId, 24));
  }

  async function exportSessionLog() {
    if (!activeTrace?.sessionId) return;
    type SaveFileHandle = {
      name: string;
      createWritable: () => Promise<{ write: (content: Blob) => Promise<void>; close: () => Promise<void> }>;
    };
    type SaveFilePicker = (options: {
      suggestedName: string;
      types: Array<{ description: string; accept: Record<string, string[]> }>;
    }) => Promise<SaveFileHandle>;
    const picker = (window as Window & { showSaveFilePicker?: SaveFilePicker }).showSaveFilePicker;
    if (!picker) {
      showToast("浏览器不支持导出", "请使用支持文件保存的 Chromium 浏览器。", "error");
      return;
    }
    let handle: SaveFileHandle;
    try {
      handle = await picker({
        suggestedName: `${activeTrace.sessionId}-${activeTrace.runId || "session"}-session-log.jsonl`,
        types: [{ description: "Session Log", accept: { "application/x-ndjson": [".jsonl"] } }],
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      showToast("Session Log 导出失败", error instanceof Error ? error.message : "无法选择保存位置", "error");
      return;
    }
    try {
      const response = await apiFetch(
        `/api/v1/sessions/${encodeURIComponent(activeTrace.sessionId)}:export`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: handle.name,
            invocationId: activeTrace.runId || undefined,
            download: true,
          }),
        },
      );
      if (!response.ok) throw new Error(`导出失败（${response.status}）`);
      const writable = await handle.createWritable();
      await writable.write(await response.blob());
      await writable.close();
      const count = response.headers.get("X-Session-Event-Count") || "0";
      showToast("Session Log 已导出", `${handle.name} · ${count} 条事件`);
    } catch (error) {
      showToast("Session Log 导出失败", error instanceof Error ? error.message : "未知错误", "error");
    }
  }

  return (
    <div
      className="page-container observability-page"
      id="traceExplorer"
      data-layout="workbench"
    >
      <PageHeaderTools>
        <div className="search-field header-search-field">
          <Search size={14} />
          <input type="search" aria-label="搜索 Trace、Run 或 Session" placeholder="搜索 Trace、Run 或 Session" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div className="segmented-control compact" aria-label="可观测时间范围">
          <button type="button" className={range === "24h" ? "selected" : ""} onClick={() => setRange("24h")}>24 小时</button>
          <button type="button" className={range === "7d" ? "selected" : ""} onClick={() => setRange("7d")}>7 天</button>
        </div>
      </PageHeaderTools>
      {activeTrace && <PageHeaderActions>
        <button className="button tertiary" type="button" onClick={() => void exportSessionLog()}>
          <Archive size={15} /><span>导出 Session Log</span>
        </button>
        <button className="button tertiary" type="button" onClick={() => {
          requestSeq.current += 1;
          setActiveTrace(null);
          setActiveSpanId(null);
          setExpanded(false);
          setDetailCollapsed(false);
        }}>
          <ArrowLeft size={15} /><span>返回 Trace 列表</span>
        </button>
      </PageHeaderActions>}

      <div className="data-page-body observability-body">
        <OverviewSection overview={overview} range={range} onRangeChange={setRange} />

        {!activeTrace && <div className="trace-toolbar" aria-label="Trace 筛选">
        <StudioSelect
          className="compact-select"
          ariaLabel="按 Agent 筛选"
          value={agentFilter || "__all__"}
          options={[
            { value: "__all__", label: "全部 Agent" },
            ...agents.map(agent => ({ value: agent.id, label: agent.name })),
          ]}
          onValueChange={value => setAgentFilter(value === "__all__" ? "" : value)}
        />
        <StudioSelect
          className="compact-select"
          ariaLabel="按状态筛选"
          value={statusFilter || "__all__"}
          options={[
            { value: "__all__", label: "全部状态" },
            { value: "COMPLETED", label: "成功" },
            { value: "FAILED", label: "失败" },
            { value: "CANCELLED", label: "已取消" },
          ]}
          onValueChange={value => setStatusFilter(value === "__all__" ? "" : value)}
        />
        <span className="trace-standard-label">OTLP · W3C Trace Context</span>
        </div>}

        {activeTrace && <section className="stat-strip" aria-label="Trace 指标">
        <div>
          <span>Trace 状态</span>
          <strong>{activeTrace ? statusLabel(activeTrace.status) : "未选择"}</strong>
          <small>{activeTrace ? `${activeTrace.spans?.length || 0} Span · ${activeTrace.target?.name || "本地工作区"}` : "选择一条 Trace 查看"}</small>
        </div>
        <div className="emphasis" data-state={activeTrace?.status === "FAILED" ? "failed" : activeTrace?.status === "COMPLETED" ? "ready" : "running"}>
          <span>总耗时</span>
          <strong>{activeTrace ? formatDuration(metrics.durationMs) : "未上报"}</strong>
          <small>{!activeTrace ? "等待 Runtime 上报" : metrics.durationMs === null || metrics.durationMs === undefined ? "Runtime 未上报" : metrics.durationSource === "runtime" ? "Runtime 精确上报" : "Studio 时钟回退"}</small>
        </div>
        <div>
          <span>Token</span>
          <strong>{activeTrace ? tokenUsageHeadline(metrics) : "未上报"}</strong>
          <small>{activeTrace && hasTokenUsage(metrics) ? tokenUsageBreakdown(metrics) : "输入 / 输出"}</small>
        </div>
        <div>
          <span>模型</span>
          <strong>{activeTrace?.model || "-"}</strong>
          <small>{activeTrace ? `${activeTrace.runtimeType || "unknown"} · ${metrics.usageSource || "Usage 未上报"}` : "Runtime"}</small>
        </div>
        </section>}

        {!activeTrace && (
          <section className="trace-list-page" aria-label="Trace 列表">
            <header className="trace-list-page-header">
              <div><strong>Traces</strong><span>服务端游标分页 · 每页 {TRACE_PAGE_SIZE} 条</span></div>
              <span>{traceTotal} 条结果</span>
            </header>
            <StudioDataTable
              columns={traceColumns}
              data={traces}
              getRowId={trace => trace.traceId}
              caption="Trace 列表"
              minWidth={1060}
              loading={traceLoading}
              error={traceError}
              onRetry={refreshTraces}
              onRowActivate={trace => openTrace(trace.traceId)}
              rowAriaLabel={trace => `${trace.agentId || "Agent"} ${trace.runId || trace.traceId}`}
              empty={{
                icon: <Activity size={20} />,
                title: "还没有 Trace",
                description: "运行一次 Agent 后在这里查看调用链，或调整筛选条件。",
              }}
              pagination={{
                pageIndex: listPage,
                pageSize: TRACE_PAGE_SIZE,
                total: traceTotal,
                hasNextPage: Boolean(nextCursor),
                onPreviousPage: previousTracePage,
                onNextPage: nextTracePage,
              }}
            />
          </section>
        )}

        {activeTrace && <div className={`trace-workbench detail-route${expanded ? " detail-expanded" : ""}${detailCollapsed ? " detail-collapsed" : ""}`}>

        <aside className="trace-run-panel" aria-label="本页 Trace">
          <div className="trace-panel-header"><div><strong>Traces</strong><span>{traceTotal} 条结果</span></div></div>
          <div className="trace-run-list">
            {traces.map(trace => {
              const agent = agents.find(item => item.id === trace.agentId);
              return (
                <button key={trace.traceId} type="button" className={trace.traceId === activeTrace.traceId ? "active" : ""} onClick={() => openTrace(trace.traceId)}>
                  <span className="trace-run-identity"><AgentAvatar name={agent?.name || trace.agentId || "Agent"} appearance={agent?.appearance} size="xs" /><span><strong>{agent?.name || trace.agentId || "Agent"}</strong><small>{shortId(trace.runId || trace.traceId, 22)}</small></span></span>
                  <span className="trace-run-meta"><span className="mono">{formatDuration(trace.durationMs)}</span><span className="badge" data-state={trace.status === "FAILED" ? "failed" : trace.status === "COMPLETED" ? "ready" : "running"} title={trace.status}>{statusLabel(trace.status)}</span></span>
                </button>
              );
            })}
          </div>
        </aside>

        <section className="trace-span-panel" aria-label="Span 时间瀑布">
          <div className="trace-panel-header trace-span-header">
            <AgentAvatar
              name={activeTraceAgent?.name || activeTrace.agentId || "Agent"}
              appearance={activeTraceAgent?.appearance}
              size="sm"
            />
            <div>
              <strong>{activeTrace ? `${activeTraceAgent?.name || activeTrace.agentId || "Agent"} · ${activeTrace.runId || "Run"}` : "选择一条 Trace"}</strong>
              <span className="mono">{activeTrace?.traceId || "-"}</span>
            </div>
            <button className="button tertiary small" type="button" disabled={!activeTrace} onClick={copyTraceparent}>
              <Copy size={14} /><span>复制 traceparent</span>
            </button>
            {detailCollapsed && (
              <button className="button secondary small trace-detail-reopen" type="button" onClick={() => setDetailCollapsed(false)}>
                <ChevronUp size={14} /><span>展开右侧详情</span>
              </button>
            )}
          </div>
          <div className="trace-axis" aria-hidden="true"><span>Span</span><span>0%</span><span>50%</span><span>100%</span><span>耗时</span></div>
          <div className="trace-span-tree">
            {orderedSpans.length === 0 ? (
              <div className="trace-stage-empty">
                <Network size={20} />
                <p>{activeTrace ? "该 OTLP Trace 没有 Span。" : "选择左侧 Trace，查看 Agent、模型和 Tool 的父子关系与耗时。"}</p>
              </div>
            ) : orderedSpans.map(({ span, depth }) => {
              let left = 0;
              let width = 0;
              try {
                const start = BigInt(span.startTimeUnixNano || "0");
                const end = BigInt(span.endTimeUnixNano || span.startTimeUnixNano || "0");
                left = Math.max(0, Math.min(100, Number(start - rootRange.start) / rootRange.duration * 100));
                width = Math.max(0, Math.min(100 - left, Number(end - start) / rootRange.duration * 100));
              } catch { /* 保持 0 */ }
              return (
                <button
                  key={span.spanId}
                  type="button"
                  className={`trace-span-row${span.spanId === activeSpanId ? " active" : ""}`}
                  data-kind={span.kind}
                  data-status={span.status}
                  onClick={() => { setActiveSpanId(span.spanId); setTab("spans"); }}
                >
                  <span className="trace-span-name">
                    <span className="trace-span-guides">{Array.from({ length: depth }, (_, i) => <span key={i} className="trace-span-guide" />)}</span>
                    <span className={`trace-span-status ${span.status}`} />
                    <span className="trace-span-name-copy"><strong>{span.name}</strong><span>{span.kind} · {shortId(span.spanId, 16)}</span></span>
                  </span>
                  <span className="trace-waterfall-track">
                    <span className="trace-waterfall-bar" style={{ "--span-left": `${left.toFixed(3)}%`, "--span-width": `${width.toFixed(3)}%` } as React.CSSProperties} />
                  </span>
                  <span className="trace-span-duration">{formatDuration(span.durationMs)}</span>
                </button>
              );
            })}
          </div>
        </section>

        <aside className={`trace-detail-panel${detailCollapsed ? " is-collapsed" : ""}`} aria-label="Span 详情">
          <div className="trace-panel-header">
            <div>
              <strong>{activeSpan ? formatField(activeSpan.name) : "Span 详情"}</strong>
              <span>{activeSpan ? `${formatField(activeSpan.kind)} · ${statusLabel(activeSpan.status)}` : "尚未选择 Span"}</span>
            </div>
            <div className="trace-detail-actions">
              {!detailCollapsed && (
                <>
                  <button className="button tertiary small" type="button" disabled={!activeTrace} onClick={copyRawOtlp}>
                    <Copy size={14} /><span>复制 Raw OTLP</span>
                  </button>
                  <button
                    className="button tertiary small trace-detail-expand"
                    type="button"
                    aria-pressed={expanded}
                    title={expanded ? "退出放大详情" : "放大详情"}
                    onClick={() => setExpanded(!expanded)}
                  >
                    {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
                    <span>{expanded ? "退出放大" : "放大详情"}</span>
                  </button>
                </>
              )}
              <button
                className="button tertiary small trace-detail-collapse"
                type="button"
                aria-expanded={!detailCollapsed}
                title={detailCollapsed ? "展开 Trace 详情" : "收起 Trace 详情"}
                onClick={() => {
                  setDetailCollapsed(value => !value);
                  if (!detailCollapsed) setExpanded(false);
                }}
              >
                {detailCollapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                <span>{detailCollapsed ? "展开详情" : "收起详情"}</span>
              </button>
            </div>
          </div>
          {!detailCollapsed && <div className="trace-tabs" role="tablist" aria-label="Trace 详情分类">
            {DETAIL_TABS.map(t => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={tab === t.id}
                className={tab === t.id ? "active" : ""}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>}
          {!detailCollapsed && <div className={`trace-detail-body${tab === "raw" ? " raw-active" : ""}`}>
            {tab === "trajectory" && (
              <div className="trace-trajectory-layout">
                <TrajectoryView
                  sessionId={activeTrace.sessionId}
                  invocationId={activeTrace.runId || undefined}
                  onSelectionChange={setSelectedTrajectory}
                />
                <aside className="trajectory-selection" aria-label="轨迹详情">
                  {selectedTrajectory ? (
                    <TrajectoryDetail record={selectedTrajectory} />
                  ) : (
                    <div className="trace-stage-empty compact"><p>选择一条轨迹事件查看详情。</p></div>
                  )}
                </aside>
              </div>
            )}
            {tab !== "raw" && tab !== "trajectory" && (
              <div>
                {!activeSpan && <div className="trace-stage-empty compact"><p>选择一个 Span 查看标准属性。</p></div>}
                {activeSpan && tab === "spans" && (
                  <>
                    <SpanContentCards span={activeSpan} />
                    <dl className="trace-detail-grid" style={{ marginTop: 14 }}>
                      <div><dt>Trace ID</dt><dd>{activeTrace?.traceId}</dd></div>
                      <div><dt>Span ID</dt><dd>{activeSpan.spanId}</dd></div>
                      <div><dt>Parent</dt><dd>{activeSpan.parentSpanId || "Root"}</dd></div>
                      <div><dt>Kind</dt><dd>{activeSpan.kind}</dd></div>
                      <div><dt>状态</dt><dd title={activeSpan.status}>{statusLabel(activeSpan.status)}</dd></div>
                      <div><dt>开始</dt><dd>{formatNanoseconds(activeSpan.startTimeUnixNano)}</dd></div>
                      <div><dt>耗时</dt><dd>{formatDuration(activeSpan.durationMs)}</dd></div>
                    </dl>
                  </>
                )}
                {activeSpan && tab === "attributes" && <KvList values={activeSpan.attributes || {}} />}
                {activeSpan && tab === "events" && <EventsView events={activeSpan.events || []} />}
                {activeSpan && tab === "resource" && (
                  <KvList values={{
                    ...(activeTrace?.resource || {}),
                    "otel.scope.name": activeTrace?.scope?.name,
                    "otel.scope.version": activeTrace?.scope?.version,
                  }} />
                )}
              </div>
            )}
            <div className="trace-raw" hidden={tab !== "raw"}>
              {tab === "raw" && !rawLoading && rawOtlp && (
                <div className="trace-raw-toolbar">
                  <span>JSON Tree</span>
                  <div role="group" aria-label="JSON 展开控制">
                    <button type="button" className={!rawExpanded ? "active" : ""} onClick={() => setRawExpanded(false)}>全部收起</button>
                    <button type="button" className={rawExpanded ? "active" : ""} onClick={() => setRawExpanded(true)}>全部展开</button>
                  </div>
                </div>
              )}
              {tab === "raw" && rawLoading && <div className="trace-raw-loading">正在读取 OTLP JSON…</div>}
              {tab === "raw" && !rawLoading && rawOtlp && (
                <div className="trace-raw-tree">
                  <JsonView
                    aria-label="Raw OTLP JSON"
                    data={rawOtlp}
                    style={OTLP_JSON_STYLES}
                    shouldExpandNode={rawExpanded ? allExpanded : collapseAllNested}
                    clickToExpandNode
                  />
                </div>
              )}
              {tab === "raw" && !rawLoading && !rawOtlp && <div className="trace-raw-loading">没有可显示的 OTLP JSON。</div>}
            </div>
          </div>}
        </aside>
        </div>}
      </div>
    </div>
  );
}

/* ================= 概览区 ================= */

function OverviewSection({ overview, range, onRangeChange }: {
  overview: TraceOverview | null;
  range: "24h" | "7d";
  onRangeChange: (range: "24h" | "7d") => void;
}) {
  const total = overview?.total || 0;
  const completed = overview?.completed || 0;
  const successRate = overview?.successRate == null
    ? "-"
    : `${Math.round(overview.successRate * 100)}%`;
  const avgDuration = overview?.averageDurationMs == null
    ? "-"
    : formatDuration(overview.averageDurationMs);
  const totalTokens = overview?.totalTokens || 0;
  const inputTokens = overview?.inputTokens || 0;
  const outputTokens = overview?.outputTokens || 0;

  return (
    <section className="observability-overview" aria-label="运行概览">
      <div className="overview-metric-grid">
        <OverviewMetric icon={<Activity size={15} />} label="总运行" value={String(total)} note={range === "7d" ? "近 7 天" : "近 24 小时"} />
        <OverviewMetric icon={<CircleCheckBig size={15} />} label="成功率" value={successRate} note={total ? `${completed} / ${total}` : "暂无运行"} tone="success" />
        <OverviewMetric icon={<Clock3 size={15} />} label="平均耗时" value={avgDuration} note={completed ? `${completed} 个完成运行` : "无完成运行"} />
        <OverviewMetric icon={<Coins size={15} />} label="Token 总量" value={formatTokenCount(totalTokens)} note={totalTokens ? `${formatTokenCount(inputTokens)} 输入 · ${formatTokenCount(outputTokens)} 输出` : "未上报"} />
      </div>
      <div className="overview-chart-card">
        <div className="overview-chart-header">
          <strong>运行趋势</strong>
          <div className="overview-range-tabs">
            <button type="button" className={range === "24h" ? "active" : ""} onClick={() => onRangeChange("24h")}>24 小时</button>
            <button type="button" className={range === "7d" ? "active" : ""} onClick={() => onRangeChange("7d")}>7 天</button>
          </div>
        </div>
        <OverviewChart buckets={overview?.buckets || []} range={range} />
        <div className="overview-chart-legend"><span className="legend-runs" />运行数<span className="legend-success" />成功数</div>
      </div>
    </section>
  );
}

function OverviewMetric({ icon, label, value, note, tone = "neutral" }: {
  icon: ReactNode;
  label: string;
  value: string;
  note: string;
  tone?: "neutral" | "success";
}) {
  return (
    <article className={`overview-metric-card ${tone}`}>
      <div className="overview-metric-label"><span>{icon}</span><small>{label}</small></div>
      <strong>{value}</strong>
      <p>{note}</p>
    </article>
  );
}

function OverviewChart({ buckets: rawBuckets, range }: {
  buckets: TraceOverview["buckets"];
  range: "24h" | "7d";
}) {
  const buckets = rawBuckets.map(bucket => {
    const date = new Date(bucket.startedAt);
    const label = range === "7d"
      ? `${date.getMonth() + 1}/${date.getDate()}`
      : `${String(date.getHours()).padStart(2, "0")}:00`;
    return { ...bucket, label, success: bucket.completed };
  });
  if (!buckets.length) return <div className="trace-stage-empty compact"><p>正在读取运行趋势…</p></div>;

  const maxRuns = Math.max(1, ...buckets.map(bucket => bucket.runs));
  const W = 800, H = 118, pad = { l: 30, r: 10, t: 10, b: 22 };
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const stepX = innerW / Math.max(1, buckets.length - 1);
  const yScale = (value: number) => pad.t + innerH - (value / maxRuns) * innerH;
  const runsPoints = buckets.map((bucket, index) => `${pad.l + index * stepX},${yScale(bucket.runs)}`).join(" ");
  const successPoints = buckets.map((bucket, index) => `${pad.l + index * stepX},${yScale(bucket.success)}`).join(" ");
  const areaPath = `M ${pad.l},${pad.t + innerH} L ${runsPoints.split(" ").join(" L ")} L ${pad.l + (buckets.length - 1) * stepX},${pad.t + innerH} Z`;
  const labelEvery = Math.ceil(buckets.length / 6);

  return (
    <svg className="overview-chart" viewBox="0 0 800 118" preserveAspectRatio="none" aria-label="运行趋势曲线">
      {[0, 0.25, 0.5, 0.75, 1].map(proportion => {
        const y = pad.t + innerH - proportion * innerH;
        return <line key={proportion} x1={pad.l} y1={y} x2={W - pad.r} y2={y} stroke="var(--border)" strokeWidth="1" strokeDasharray={proportion === 0 ? "0" : "3 3"} />;
      })}
      <path d={areaPath} fill="var(--accent-soft)" opacity="0.6" />
      <polyline points={runsPoints} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      <polyline points={successPoints} fill="none" stroke="var(--success)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" strokeDasharray="4 3" />
      {buckets.map((bucket, index) => (
        <circle key={bucket.startedAt} cx={pad.l + index * stepX} cy={yScale(bucket.runs)} r="2.5" fill="var(--accent)">
          <title>{`${bucket.label}：${bucket.runs} 次运行，${bucket.success} 次成功`}</title>
        </circle>
      ))}
      {buckets.map((bucket, index) => (index % labelEvery === 0 || index === buckets.length - 1) && (
        <text key={`label-${bucket.startedAt}`} x={pad.l + index * stepX} y={H - 8} textAnchor="middle" fill="var(--text-tertiary)" fontSize="11">{bucket.label}</text>
      ))}
    </svg>
  );
}

/* ================= Events 视图（delta 聚合 + 普通事件） ================= */

function EventsView({ events }: { events: SpanEvent[] }) {
  const groups = useMemo(() => {
    const out: Array<{ type: "card"; card: ContentCard } | { type: "event"; event: SpanEvent }> = [];
    let buf = "";
    let kind: "thinking" | "message" | null = null;
    let segments = 0;
    const flush = () => {
      if (kind && buf.trim()) out.push({ type: "card", card: { kind, text: buf, segments, final: false } });
      buf = ""; segments = 0; kind = null;
    };
    for (const e of events) {
      if (e.name === "thinking.delta" || e.name === "message.delta") {
        const k = e.name === "thinking.delta" ? "thinking" : "message";
        if (kind !== k) flush();
        kind = k;
        buf += eventContent(e.attributes);
        segments += 1;
      } else if (e.name === "thinking.completed" || e.name === "message.completed") {
        // completed 事件内容已在概览内容卡片中体现，跳过避免重复
      } else {
        flush();
        out.push({ type: "event", event: e });
      }
    }
    flush();
    return out;
  }, [events]);

  if (!events.length) return <div className="trace-stage-empty compact"><p>该 Span 没有 Events。</p></div>;
  return (
    <div className="io-stack">
      {groups.map((g, i) => g.type === "card" ? (
        <IoBlock
          key={i}
          label={g.card.kind === "message" ? "模型输出流" : "思考流"}
          text={g.card.text}
          tone={g.card.kind}
          icon={g.card.kind === "message" ? <MessageSquare size={12} /> : <Brain size={12} />}
          meta={`${g.card.segments} 段增量`}
        />
      ) : (
        <article key={i} className="trace-event-card">
          <strong>{g.event.name}</strong>
          <span>{formatNanoseconds(g.event.timeUnixNano)}</span>
          <KvList values={g.event.attributes || {}} />
        </article>
      ))}
    </div>
  );
}
