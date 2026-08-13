import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowRight,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Coins,
  Cpu,
  Gauge,
  GitBranch,
  Loader2,
  MessageSquare,
  Play,
  RefreshCw,
  ShieldCheck,
  Terminal,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { apiFetch } from "../api";
import {
  createResponseSseParser,
  projectRunInspectorTimeline,
  type RunEvent,
  type RunInspectorTimelineItem,
} from "../chatProtocol";

interface RunRecord {
  id: string;
  agentId: string;
  sessionId: string;
  traceId: string;
  model: string;
  runtimeType?: string;
  status: string;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number | null;
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
    reported?: boolean;
  };
  error?: { code?: string; message?: string } | null;
}

interface Span {
  spanId: string;
  parentSpanId?: string;
  name: string;
  kind: string;
  status: string;
  durationMs: number;
  startTimeUnixNano?: string;
}

interface WaterfallSpan extends Span {
  left: number;
  width: number;
  depth: number;
}

function fmtDuration(ms?: number | null): string {
  if (!Number.isFinite(ms) || Number(ms) < 0) return "未上报";
  if (Number(ms) < 1000) return `${Math.round(Number(ms))}ms`;
  return `${(Number(ms) / 1000).toFixed(2)}s`;
}

function fmtTokens(value?: number): string {
  return Number.isFinite(value) ? Number(value).toLocaleString() : "未上报";
}

function shortId(id: string): string {
  return id.length > 20 ? `${id.slice(0, 17)}…` : id;
}

function statusLabel(status: string): string {
  if (status === "RUNNING" || status === "CREATED") return "运行中";
  if (status === "COMPLETED") return "已完成";
  if (status === "CANCELLED") return "已取消";
  if (status === "INTERRUPTED") return "已中断";
  if (status === "TIMED_OUT") return "已超时";
  return "失败";
}

function startNs(span: Span): bigint {
  try { return BigInt(String(span.startTimeUnixNano || "0")); } catch { return 0n; }
}

function waterfallLayout(spans: Span[]): WaterfallSpan[] {
  if (!spans.length) return [];
  const ordered = [...spans].sort((left, right) => startNs(left) < startNs(right) ? -1 : 1);
  const base = ordered.reduce((minimum, span) => {
    const value = startNs(span);
    return !minimum || value < minimum ? value : minimum;
  }, 0n);
  const offsets = ordered.map(span => Number(startNs(span) - base) / 1_000_000);
  const total = Math.max(1, ...ordered.map((span, index) => offsets[index] + Math.max(0, Number(span.durationMs) || 0)));
  const byId = new Map(ordered.map(span => [span.spanId, span]));
  const depthOf = (span: Span) => {
    let depth = 0;
    let parentId = span.parentSpanId;
    const seen = new Set<string>();
    while (parentId && byId.has(parentId) && !seen.has(parentId) && depth < 3) {
      seen.add(parentId);
      depth += 1;
      parentId = byId.get(parentId)?.parentSpanId;
    }
    return depth;
  };
  return ordered.slice(0, 8).map((span, index) => ({
    ...span,
    left: Math.min(96, Math.max(0, offsets[index] / total * 100)),
    width: Math.max(3, Math.min(100, Math.max(0, Number(span.durationMs) || 0) / total * 100)),
    depth: depthOf(span),
  }));
}

function timelineIcon(item: RunInspectorTimelineItem) {
  if (item.kind === "thinking") return <BrainCircuit size={13} />;
  if (item.kind === "message") return <MessageSquare size={13} />;
  if (item.kind === "command") return <Terminal size={13} />;
  if (item.kind === "tool") return <Wrench size={13} />;
  if (item.kind === "approval") return <ShieldCheck size={13} />;
  if (item.kind === "usage") return <Gauge size={13} />;
  return <Play size={13} />;
}

async function loadEvents(runId: string): Promise<RunEvent[]> {
  const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/events`);
  if (!response.ok) return [];
  const parsed: RunEvent[] = [];
  const parser = createResponseSseParser(event => {
    const { type, ...data } = event;
    parsed.push({ id: parsed.length + 1, type, data });
  });
  parser.push(await response.text());
  parser.finish();
  return parsed;
}

/** 会话页右侧运行检查器：状态、用量、Trace 瀑布与压缩事件时间线。 */
export function ChatRunPanel({ agentId, onOpenTrace, onClose }: { agentId: string; onOpenTrace: () => void; onClose: () => void }) {
  const [latest, setLatest] = useState<RunRecord | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [spans, setSpans] = useState<Span[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    let requestId = 0;
    async function load() {
      const currentRequest = ++requestId;
      try {
        const runPayload = await apiFetch("/api/v1/runs").then(response => response.json());
        const matches: RunRecord[] = (runPayload.items || []).filter((run: RunRecord) => !agentId || run.agentId === agentId);
        const current = matches.at(-1) || null;
        if (cancelled || currentRequest !== requestId) return;
        setLatest(current);
        if (!current) {
          setEvents([]);
          setSpans([]);
          return;
        }
        const [nextEvents, trace] = await Promise.all([
          loadEvents(current.id),
          apiFetch(`/api/v1/traces/${encodeURIComponent(current.traceId)}`).then(response => response.ok ? response.json() : null).catch(() => null),
        ]);
        if (!cancelled && currentRequest === requestId) {
          setEvents(nextEvents);
          setSpans(trace?.spans || []);
        }
      } catch {
        // Inspector 是增强视图，保留上次可用数据，避免遮断会话。
      } finally {
        if (!cancelled && currentRequest === requestId) setLoading(false);
        if (!cancelled) timer = window.setTimeout(load, 2500);
      }
    }
    setLoading(true);
    void load();
    return () => {
      cancelled = true;
      requestId += 1;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [agentId, refreshKey]);

  const running = latest?.status === "RUNNING" || latest?.status === "CREATED";
  const failed = latest ? /fail|error|interrupt|timed/i.test(latest.status) : false;
  const timeline = useMemo(() => projectRunInspectorTimeline(events), [events]);
  const waterfall = useMemo(() => waterfallLayout(spans), [spans]);
  const usageReported = latest?.usage?.reported === true;

  return (
    <aside className="chat-run-panel" aria-label="运行检查器">
      <div className="chat-run-head">
        <span className="chat-run-title"><Activity size={15} /> 运行检查器</span>
        <span className="chat-run-head-spacer" />
        <button className="icon-btn" onClick={() => setRefreshKey(value => value + 1)} title="刷新"><RefreshCw size={14} /></button>
        <button className="icon-btn" onClick={onClose} title="收起"><X size={14} /></button>
      </div>

      {loading && !latest ? (
        <div className="chat-run-empty"><Loader2 size={16} className="animate-spin" /> 正在读取最近运行…</div>
      ) : !latest ? (
        <div className="chat-run-empty">发送一条消息后，这里会显示执行位置、用量与事件时间线。</div>
      ) : (
        <>
          <div className="chat-run-scroll">
            <section className="chat-run-overview">
              <div className="chat-run-status-row">
                <span className={`chat-run-state-icon ${running ? "running" : failed ? "failed" : "completed"}`}>
                  {running ? <Loader2 size={15} className="animate-spin" /> : failed ? <XCircle size={15} /> : <CheckCircle2 size={15} />}
                </span>
                <div className="chat-run-identity">
                  <strong>{statusLabel(latest.status)}</strong>
                  <span title={latest.id}>{shortId(latest.id)}</span>
                </div>
                <span className={`chat-run-state ${running ? "running" : failed ? "failed" : "completed"}`}>{latest.status}</span>
              </div>
              {latest.error?.message && (
                <div className="chat-run-error" role="alert">
                  <XCircle size={15} />
                  <div><strong>{latest.error.code || "运行失败"}</strong><span>{latest.error.message}</span></div>
                </div>
              )}
              <div className="chat-run-route">
                <Cpu size={13} />
                <span>Edge · Local</span><i />
                <span>{latest.runtimeType || "codex"} Runtime</span><i />
                <span>{latest.model || "未指定模型"}</span>
              </div>
            </section>

            <section className="chat-run-section">
              <div className="chat-run-section-title"><span>本次运行</span><small>{latest.startedAt ? new Date(latest.startedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}</small></div>
              <div className="chat-run-metrics">
                <div><Clock3 size={13} /><span>耗时</span><strong>{fmtDuration(latest.durationMs)}</strong></div>
                <div><Coins size={13} /><span>总 Token</span><strong>{usageReported ? fmtTokens(latest.usage?.totalTokens) : "未上报"}</strong></div>
                <div><MessageSquare size={13} /><span>输入 / 输出</span><strong>{usageReported ? `${fmtTokens(latest.usage?.inputTokens)} / ${fmtTokens(latest.usage?.outputTokens)}` : "未上报"}</strong></div>
                <div><GitBranch size={13} /><span>Span</span><strong>{spans.length || "—"}</strong></div>
              </div>
            </section>

            <section className="chat-run-section">
              <div className="chat-run-section-title"><span>Trace 瀑布</span><small>{waterfall.length ? `${waterfall.length} 个节点` : "等待 Span"}</small></div>
              {waterfall.length ? (
                <div className="chat-run-waterfall">
                  {waterfall.map(span => {
                    const spanFailed = /fail|error/i.test(span.status);
                    return (
                      <div className="chat-run-waterfall-row" key={span.spanId}>
                        <span className="chat-run-waterfall-label" style={{ paddingLeft: span.depth * 8 }} title={span.name}>{span.name}</span>
                        <span className="chat-run-waterfall-track">
                          <i className={spanFailed ? "failed" : ""} style={{ left: `${span.left}%`, width: `${span.width}%` }} />
                        </span>
                        <small>{fmtDuration(span.durationMs)}</small>
                      </div>
                    );
                  })}
                </div>
              ) : <div className="chat-run-inline-empty">运行开始后显示 Span 时序</div>}
            </section>

            <section className="chat-run-section chat-run-events-section">
              <div className="chat-run-section-title"><span>执行事件</span><small>{timeline.length}</small></div>
              <div className="chat-run-timeline">
                {timeline.length ? timeline.map(item => (
                  <div className={`chat-run-event ${item.kind} ${item.status}`} key={item.id}>
                    <span className="chat-run-event-icon">{timelineIcon(item)}</span>
                    <div className="chat-run-event-copy">
                      <div><strong>{item.title}</strong><small>{item.summary}</small></div>
                      {item.detail && (
                        <details>
                          <summary>查看详情</summary>
                          <pre>{item.detail}</pre>
                        </details>
                      )}
                    </div>
                  </div>
                )) : <div className="chat-run-inline-empty">正在建立 Runtime 连接…</div>}
              </div>
            </section>
          </div>

          <div className="chat-run-footer">
            <button className="btn soft" onClick={onOpenTrace}>
              打开完整 Trace <ArrowRight size={14} />
            </button>
          </div>
        </>
      )}
    </aside>
  );
}
