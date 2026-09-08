import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowRight,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
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

interface ContextEvidence {
  plannedInputTokens?: number | null;
  projectedInputTokens?: number | null;
  tokensByKind?: Record<string, number>;
  decisions?: Array<{ decision?: string; kind?: string; reason?: string }>;
}

interface PromptEvidence {
  sectionCount?: number | null;
  tokensBySection?: Record<string, number>;
}

interface PromptReveal {
  available: boolean;
  reason?: string;
  sections?: Array<{ id: string; source?: string; content: string }>;
}

export interface MemoryRecallPresentation {
  status: "used" | "recalled" | "empty" | "failed" | "unused";
  title: string;
  description: string;
}

/**
 * 长期记忆的用户可见状态以 Runtime 真实事件为准。
 * Native Runner 的 ContextPlan 只代表平台投影，可能不会精确回填 recalled_memory token。
 */
export function resolveMemoryRecallPresentation(
  events: RunEvent[],
  recalledMemoryTokens?: number | null,
): MemoryRecallPresentation {
  const recallEvents = events.filter(event => event.type.startsWith("memory.recall."));
  const projected = [...recallEvents].reverse().find(event => event.type === "memory.recall.projected");
  if (projected) {
    const count = Number(projected.data?.candidate_count ?? projected.data?.count ?? 0);
    return {
      status: "used",
      title: "已提供长期记忆",
      description: count > 0 ? `${count} 条相关记忆已交付本次运行` : "相关记忆已交付本次运行",
    };
  }
  const completed = [...recallEvents].reverse().find(event => event.type === "memory.recall.completed");
  if (completed) {
    const count = Number(completed.data?.candidate_count ?? completed.data?.count ?? 0);
    return {
      status: "recalled",
      title: "已召回长期记忆",
      description: count > 0 ? `已找到 ${count} 条，但未确认交付 Runner` : "已找到相关记忆，但未确认交付 Runner",
    };
  }
  if (recallEvents.some(event => event.type === "memory.recall.failed")) {
    return {
      status: "failed",
      title: "长期记忆召回失败",
      description: "本次未能读取长期记忆，可在 Trace 中查看原因",
    };
  }
  if (recallEvents.some(event => event.type === "memory.recall.empty")) {
    return {
      status: "empty",
      title: "未使用长期记忆",
      description: "未找到与当前问题相关的记忆",
    };
  }
  if (Number(recalledMemoryTokens || 0) > 0) {
    return {
      status: "used",
      title: "已纳入长期记忆",
      description: "相关记忆已纳入本次上下文",
    };
  }
  return {
    status: "unused",
    title: "未使用长期记忆",
    description: "本次回答未选入长期记忆",
  };
}

function fmtDuration(ms?: number | null): string {
  if (!Number.isFinite(ms) || Number(ms) < 0) return "未上报";
  if (Number(ms) < 1000) return `${Math.round(Number(ms))}ms`;
  return `${(Number(ms) / 1000).toFixed(2)}s`;
}

function fmtTokens(value?: number): string {
  return Number.isFinite(value) ? Number(value).toLocaleString() : "未上报";
}

function promptSectionLabel(section: string): string {
  const labels: Record<string, string> = {
    platform_safety: "平台安全规则",
    agent_identity: "角色定义",
    agent_policy: "任务规则",
    runtime_capabilities: "运行时能力说明",
    resource_manifest: "工具与 Skill 说明",
    request_instructions: "本次请求指令",
  };
  return labels[section] || section;
}

function promptSectionSourceLabel(section: string): string {
  const labels: Record<string, string> = {
    platform_safety: "平台策略",
    agent_identity: "Agent Revision",
    agent_policy: "Agent Revision",
    runtime_capabilities: "Runtime Adapter",
    resource_manifest: "构建资源清单",
    request_instructions: "本次请求",
  };
  return labels[section] || "Prompt Compiler";
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
  const [contextEvidence, setContextEvidence] = useState<ContextEvidence | null>(null);
  const [promptEvidence, setPromptEvidence] = useState<PromptEvidence | null>(null);
  const [promptReveal, setPromptReveal] = useState<PromptReveal | null>(null);
  const [promptRevealLoading, setPromptRevealLoading] = useState(false);
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
          setContextEvidence(null);
          setPromptEvidence(null);
          return;
        }
        const [nextEvents, trace, context, prompt] = await Promise.all([
          loadEvents(current.id),
          apiFetch(`/api/v1/traces/${encodeURIComponent(current.traceId)}`).then(response => response.ok ? response.json() : null).catch(() => null),
          apiFetch(`/api/v1/runs/${encodeURIComponent(current.id)}/context`).then(response => response.ok ? response.json() : null).catch(() => null),
          apiFetch(`/api/v1/runs/${encodeURIComponent(current.id)}/prompt`).then(response => response.ok ? response.json() : null).catch(() => null),
        ]);
        if (!cancelled && currentRequest === requestId) {
          setEvents(nextEvents);
          setSpans(trace?.spans || []);
          setContextEvidence(context);
          setPromptEvidence(prompt);
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

  useEffect(() => {
    setPromptReveal(null);
    setPromptRevealLoading(false);
  }, [latest?.id]);

  async function revealPrompt(runId: string) {
    if (promptReveal || promptRevealLoading) return;
    setPromptRevealLoading(true);
    try {
      const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/prompt?include_content=true`);
      const payload = response.ok ? await response.json() : null;
      setPromptReveal(payload?.reveal || { available: false, reason: "Prompt 详情读取失败。" });
    } catch {
      setPromptReveal({ available: false, reason: "Prompt 详情读取失败。" });
    } finally {
      setPromptRevealLoading(false);
    }
  }

  const running = latest?.status === "RUNNING" || latest?.status === "CREATED";
  const failed = latest ? /fail|error|interrupt|timed/i.test(latest.status) : false;
  const timeline = useMemo(() => projectRunInspectorTimeline(events), [events]);
  const waterfall = useMemo(() => waterfallLayout(spans), [spans]);
  const usageReported = latest?.usage?.reported === true;
  const decisions = contextEvidence?.decisions || [];
  const hasRunEvidence = Boolean(contextEvidence || promptEvidence || usageReported);
  const contextStatus = decisions.some(item => item.decision === "dropped")
    ? "部分内容已舍弃"
    : decisions.some(item => item.decision === "compressed")
      ? "已自动压缩"
      : decisions.some(item => item.decision === "replaced")
        ? "部分内容已替换"
      : hasRunEvidence
        ? "正常"
        : "等待证据";
  const promptSectionNames = Object.keys(promptEvidence?.tokensBySection || {}).map(promptSectionLabel);
  const recalledMemoryTokens = contextEvidence?.tokensByKind?.recalled_memory;
  const memoryRecall = useMemo(
    () => resolveMemoryRecallPresentation(events, recalledMemoryTokens),
    [events, recalledMemoryTokens],
  );
  const contextAdjusted = Number.isFinite(contextEvidence?.plannedInputTokens)
    && Number.isFinite(contextEvidence?.projectedInputTokens)
    && Number(contextEvidence?.plannedInputTokens) !== Number(contextEvidence?.projectedInputTokens);

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

            <section className="chat-run-section pcm-run-summary">
              <div className="chat-run-section-title"><span>运行解释</span><small>{contextStatus}</small></div>
              <div className={`pcm-run-health ${contextStatus === "正常" ? "healthy" : contextStatus === "等待证据" ? "pending" : "adjusted"}`}>
                {contextStatus === "正常" ? <CheckCircle2 size={16} /> : <Gauge size={16} />}
                <div>
                  <strong>{contextStatus === "正常" ? "本次运行依据已正常准备" : contextStatus}</strong>
                  <span>{contextEvidence
                    ? "规则、相关记忆和当前问题已按策略处理"
                    : promptEvidence && usageReported
                      ? "规则证据与模型用量已记录；Runner 内部上下文由框架管理"
                      : "正在收集本次运行依据"}</span>
                </div>
              </div>

              <div className="pcm-run-signal-list">
                <details className="pcm-run-signal" onToggle={event => {
                  if (event.currentTarget.open) void revealPrompt(latest.id);
                }}>
                  <summary>
                    <CheckCircle2 size={14} />
                    <div><strong>规则已应用</strong><span>{promptSectionNames.length ? promptSectionNames.join(" · ") : promptEvidence?.sectionCount ? `${promptEvidence.sectionCount} 个规则来源` : "本次未提供规则来源证据"}</span></div>
                    <ChevronDown className="pcm-run-signal-chevron" size={14} />
                  </summary>
                  <div className="pcm-run-signal-details">
                    {promptRevealLoading ? <p>正在按本次不可变 Build 校验并读取 Prompt…</p>
                      : promptReveal?.available && promptReveal.sections?.length ? promptReveal.sections.map(section => (
                        <div className="pcm-run-prompt-section" key={section.id}>
                          <span><strong>{promptSectionLabel(section.id)}</strong><small>来源：{promptSectionSourceLabel(section.id)}</small></span>
                          <pre>{section.content}</pre>
                        </div>
                      )) : <p>{promptReveal?.reason || "展开后按需读取 Prompt 正文；正文不会写入 Trace。"}</p>}
                  </div>
                </details>
                <div className={`pcm-run-signal-static ${memoryRecall.status === "failed" ? "attention" : ""}`}>
                  <BrainCircuit size={14} />
                  <div><strong>{memoryRecall.title}</strong><span>{memoryRecall.description}</span></div>
                </div>
                <div className={`pcm-run-signal-static ${contextStatus !== "正常" && contextStatus !== "等待证据" ? "attention" : ""}`}>
                  <Gauge size={14} />
                  <div>
                    <strong>{contextAdjusted ? "上下文已按预算调整" : decisions.some(item => item.decision === "compressed" || item.decision === "dropped" || item.decision === "replaced") ? contextStatus : "上下文无需压缩"}</strong>
                    <span>{contextAdjusted ? "关键规则与当前问题已优先保留" : "未检测到压缩、替换或舍弃"}</span>
                  </div>
                </div>
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
