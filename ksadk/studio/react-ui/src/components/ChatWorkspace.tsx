import {
  Children,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bot,
  BrainCircuit,
  Check,
  ChevronDown,
  Copy,
  Loader2,
  MessageSquarePlus,
  PanelLeftOpen,
  Pause,
  Play,
  Send,
  ShieldAlert,
  Terminal,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { apiFetch } from "../api";
import {
  approvalModeStorageKey,
  normalizeApprovalMode,
  type ApprovalMode,
} from "../approvalModes";
import {
  applyConversationStreamResult,
  createChatStreamState,
  createResponseSseParser,
  eventDetail,
  groupRunsBySession,
  latestActiveRun,
  persistedRunsForDisplay,
  projectA2UISurfaces,
  projectRunActivities,
  reduceChatStreamEvent,
  contextUsageState,
  contextUsageTooltip,
  latestReportedInputTokens,
  type ChatRun,
  type ChatStreamState,
  type A2UISurface,
  type RunActivity,
  type RunEvent,
} from "../chatProtocol";
import { A2UIRenderer } from "./A2UIRenderer";
import { AgentAvatar, type AgentAppearance } from "./AgentAvatar";
import { ConfirmDialog } from "./ConfirmDialog";
import { showToast } from "./Toast";
import {
  buildResponsesInput,
  COMPOSER_ATTACHMENT_ACCEPT,
  encodedComposerAttachmentsBytes,
  fileToComposerAttachment,
  MAX_COMPOSER_ATTACHMENT_BYTES,
  MAX_COMPOSER_ATTACHMENTS,
  parseComposerSubmission,
  type CollaborationMode,
  type ComposerAttachment,
  type ComposerCommand,
} from "../composerActions";
import {
  ChatComposer,
  type ComposerModelOption,
  type ReasoningEffort,
} from "./ChatComposer";
import { RuntimeModeBar, type RuntimeMode, type RuntimeModeStatus } from "./RuntimeModeBar";
import { redactTechnicalError, runErrorCopy } from "../utils/chatErrors";
import {
  buildConversationInput,
  ConversationClientError,
  HttpConversationClient,
  surfacePermitsInput as declaredSurfacePermitsInput,
  type ConversationTimelineEntry,
  type ConversationSurface,
} from "../conversationProtocol";

function conversationFailureMessage(error: unknown): string {
  if (!(error instanceof ConversationClientError)) {
    return error instanceof Error ? error.message : String(error);
  }
  switch (error.code) {
    case "conversation_run_identity_missing":
      return "会话流在返回可恢复的 Run 标识前中断，请刷新会话查看运行结果。";
    case "conversation_reconnect_exhausted":
      return "会话流已断开，自动续流后仍未到达终态；运行仍在后台继续，请刷新会话查看结果。";
    case "conversation_input_unsupported":
      return "当前 Agent 不支持本轮选择的输入能力，请调整附件、模型或运行模式后重试。";
    case "conversation_session_mismatch":
      return "会话已发生变化，请刷新后重试。";
    case "conversation_contract_mismatch":
      return "Agent 返回的会话数据与当前协议不兼容，请刷新或改用旧版兼容入口。";
    case "conversation_http_error":
    case "conversation_stream_error":
      return "云端会话连接中断，请稍后重试；已开始的运行仍可从会话记录恢复。";
    case "conversation_aborted":
      return "本轮会话已停止。";
  }
}

interface ChatModel {
  id: string;
  display_name?: string;
  displayName?: string;
  context_window_tokens?: number;
  contextWindowTokens?: number;
  capabilities?: Record<string, unknown>;
}

type ConversationSurfaceState =
  | { status: "loading" }
  | { status: "declared"; buildId: string; surface: ConversationSurface }
  | { status: "legacy" }
  | { status: "error" };

function surfacePermitsInput(
  state: ConversationSurfaceState,
  ...names: string[]
): boolean {
  if (state.status === "legacy") return true;
  if (state.status !== "declared") return false;
  return declaredSurfacePermitsInput(state.surface, ...names);
}

interface ChatWorkspaceProps {
  agentId: string;
  agentName: string;
  agentAppearance?: AgentAppearance;
  active?: boolean;
  refreshTick?: number;
  onRunChanged?: () => void;
  onConfigureAgent?: () => void;
  onOpenSettings?: () => void;
}

function explicitReasoningEfforts(model?: ChatModel): ReasoningEffort[] {
  const raw = model?.capabilities?.reasoning_efforts;
  if (!Array.isArray(raw)) return [];
  return raw.filter((value): value is ReasoningEffort => (
    value === "low" || value === "medium" || value === "high"
  ));
}

function ContextRing({
  usedTokens,
  limitTokens,
  known,
  percent,
}: ReturnType<typeof contextUsageState>) {
  const tooltipId = useId();
  const tooltip = contextUsageTooltip({ usedTokens, limitTokens, known, percent });
  const accessibleLabel = `${tooltip.title}：${tooltip.value}，${tooltip.detail}`;
  return (
    <span
      className={`chat-context-ring${known ? "" : " unknown"}`}
      role="img"
      aria-label={accessibleLabel}
      aria-describedby={tooltipId}
      tabIndex={0}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle className="chat-context-track" cx="12" cy="12" r="8.5" pathLength="100" />
        <circle
          className="chat-context-value"
          cx="12"
          cy="12"
          r="8.5"
          pathLength="100"
          strokeDasharray={`${known ? percent : 12} ${known ? 100 - percent : 88}`}
        />
      </svg>
      <span id={tooltipId} role="tooltip" className="chat-context-tooltip">
        <span>{tooltip.title}</span>
        <strong>{tooltip.value}</strong>
        <small>{tooltip.detail}</small>
      </span>
    </span>
  );
}

function uniqueId(prefix: string): string {
  const value = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `${prefix}_${value}`;
}

function formatSessionTime(value: string): string {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
}

function formatMessageTime(value?: string): string {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function shortText(value: string, limit = 34): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > limit ? `${normalized.slice(0, limit)}…` : normalized;
}

function formatRunDuration(durationMs?: number | null): string {
  if (!Number.isFinite(durationMs) || Number(durationMs) < 0) return "";
  const seconds = Number(durationMs) / 1000;
  return seconds < 10 ? `${seconds.toFixed(1)} 秒` : `${Math.round(seconds)} 秒`;
}

async function responseError(response: Response): Promise<string> {
  const payload = await response.clone().json().catch(() => null);
  return payload?.error?.message
    || payload?.Message
    || payload?.message
    || `请求失败（HTTP ${response.status}）`;
}

async function uploadConversationAttachment(
  attachment: ComposerAttachment,
  signal: AbortSignal,
): Promise<{ attachmentRef: string; mediaType: string; name: string }> {
  let blob: Blob;
  if (attachment.kind === "image" && attachment.dataUrl) {
    blob = await (await fetch(attachment.dataUrl, { signal })).blob();
  } else {
    blob = new Blob([attachment.text || ""], { type: attachment.mimeType || "text/plain" });
  }
  const form = new FormData();
  form.append("file", new File([blob], attachment.name, {
    type: attachment.mimeType || blob.type || "application/octet-stream",
  }));
  const response = await apiFetch("/api/v1/conversation-attachments", {
    method: "POST",
    body: form,
    signal,
  });
  if (!response.ok) throw new Error(await responseError(response));
  const payload = await response.json();
  if (typeof payload?.attachmentRef !== "string"
    || typeof payload?.mediaType !== "string"
    || typeof payload?.name !== "string") {
    throw new Error("附件上传响应无效");
  }
  return payload;
}

function RunErrorCard({
  error,
  onConfigure,
  onOpenSettings,
  onRetry,
}: {
  error: string;
  onConfigure?: () => void;
  onOpenSettings?: () => void;
  onRetry?: () => void;
}) {
  const copy = runErrorCopy(error);
  const configure = copy.recoverable === "credential" ? onOpenSettings : onConfigure;
  return (
    <div className="chat-run-error" role="alert">
      <span className="chat-run-error-icon"><ShieldAlert size={17} /></span>
      <div className="chat-run-error-copy">
        <strong>{copy.title}</strong>
        <p>{copy.message}</p>
        <div className="chat-run-error-actions">
          {copy.recoverable !== "retry" && configure && (
            <button className="button secondary small" type="button" onClick={configure}>
              {copy.recoverable === "credential" ? "配置凭证" : "配置 Agent"}
            </button>
          )}
          {onRetry && <button className="button secondary small" type="button" onClick={onRetry}>重新运行</button>}
        </div>
        <details className="chat-run-error-detail">
          <summary>技术详情</summary>
          <pre>{redactTechnicalError(error)}</pre>
        </details>
      </div>
    </div>
  );
}

function activityIcon(kind: RunActivity["kind"]) {
  if (kind === "command") return <Terminal size={14} />;
  if (kind === "approval") return <ShieldAlert size={14} />;
  return <Wrench size={14} />;
}

function activityLabel(activity: RunActivity): string {
  if (activity.kind === "command") return "命令";
  if (activity.kind === "approval") return "人工确认";
  return "工具";
}

function reasoningPreview(value: string): string {
  const normalized = value.replace(/```[\s\S]*?```/g, " ").replace(/[`#>*_[\]()-]+/g, " ").replace(/\s+/g, " ").trim();
  if (!normalized) return "";
  const parts = normalized.split(/[。！？!?]+|\.(?=\s|$)/).map(item => item.trim()).filter(Boolean);
  return shortText(parts.at(-1) || normalized, 72);
}

function ProcessingGroup({
  reasoning,
  activities,
  streaming = false,
  durationMs,
}: {
  reasoning: string;
  activities: RunActivity[];
  streaming?: boolean;
  durationMs?: number | null;
}) {
  if (!reasoning && activities.length === 0) return null;
  const running = activities.find(activity => activity.status === "running" || activity.status === "waiting");
  const duration = formatRunDuration(durationMs);
  const title = streaming
    ? running ? `${running.status === "waiting" ? "等待确认" : "正在处理"} · ${running.title}`
      : reasoningPreview(reasoning) ? `正在思考 · ${reasoningPreview(reasoning)}`
        : "正在思考"
    : duration ? `已思考（用时 ${duration}）`
      : activities.length > 0 ? `已完成思考 · ${activities.length} 项操作`
        : "查看思考过程";
  return (
    <details className="chat-processing-group" open={streaming} data-ui="think">
      <summary>
        <BrainCircuit size={15} className="chat-processing-icon" />
        <span>{title}</span>
        {streaming && <Loader2 size={13} className="animate-spin" />}
        <ChevronDown size={14} className="details-chevron" />
      </summary>
      <div className="chat-processing-content">
        {reasoning && <div className="chat-reasoning-content">{reasoning}</div>}
        {activities.map(activity => <ActivityCard key={activity.id} activity={activity} />)}
      </div>
    </details>
  );
}

function ActivityCard({ activity }: { activity: RunActivity }) {
  const expandable = Boolean(activity.detail) || Object.keys(activity.data).length > 2;
  const row = (
    <>
      <span className="chat-activity-icon">{activityIcon(activity.kind)}</span>
      <span className="chat-activity-copy">
        <small>{activityLabel(activity)}</small>
        <strong>{activity.title}</strong>
      </span>
      <span className={`chat-activity-status ${activity.status}`}>{
        activity.status === "completed" ? "已完成"
          : activity.status === "failed" ? "失败"
            : activity.status === "waiting" ? "等待确认"
              : "运行中"
      }</span>
    </>
  );
  if (!expandable) {
    return <div className={`chat-activity-card ${activity.kind}`}><div className="chat-activity-row">{row}</div></div>;
  }
  return (
    <details className={`chat-activity-card ${activity.kind}`}>
      <summary>
        {row}
        <ChevronDown size={14} className="details-chevron" />
      </summary>
      <pre>{activity.detail || JSON.stringify(activity.data, null, 2)}</pre>
    </details>
  );
}

/**
 * Render the canonical item order for typed Conversation/v1 streams.  This is
 * deliberately separate from the legacy ProcessingGroup: a grouped string
 * cannot represent thinking → tool → thinking → answer without reordering it.
 */
function ConversationTimeline({
  items,
  streaming,
}: {
  items: ConversationTimelineEntry[];
  streaming: boolean;
}) {
  const visible = items.filter(entry => !["artifact", "a2ui", "error"].includes(entry.item.kind));
  if (!visible.length) return null;
  return (
    <div className="chat-conversation-timeline" data-ui="conversation-timeline">
      {visible.map(entry => {
        const { item } = entry;
        if (item.kind === "assistant_text") {
          const text = typeof item.payload.text === "string" ? item.payload.text : "";
          return text ? <MarkdownMessage key={entry.key} streaming={streaming && item.lifecycle === "streaming"}>{text}</MarkdownMessage> : null;
        }
        if (item.kind === "reasoning") {
          const text = typeof item.payload.text === "string" ? item.payload.text : "";
          return (
            <details className="chat-processing-group" key={entry.key} open={streaming && item.lifecycle !== "completed"} data-ui="think">
              <summary>
                <BrainCircuit size={15} className="chat-processing-icon" />
                <span>{item.lifecycle === "completed" ? "查看思考过程" : "正在思考"}</span>
                {streaming && item.lifecycle !== "completed" && <Loader2 size={13} className="animate-spin" />}
                <ChevronDown size={14} className="details-chevron" />
              </summary>
              {text && <div className="chat-processing-content"><div className="chat-reasoning-content">{text}</div></div>}
            </details>
          );
        }
        if (item.kind === "tool_call" || item.kind === "approval") {
          const pending = item.kind === "approval" && item.lifecycle === "pending";
          const activity: RunActivity = {
            id: entry.key,
            kind: item.kind === "approval" ? "approval" : "tool",
            title: String(item.payload.tool || item.payload.title || item.payload.kind || (pending ? "等待批准" : "调用工具")),
            status: item.lifecycle === "failed" ? "failed" : item.lifecycle === "completed" ? "completed" : pending ? "waiting" : "running",
            detail: eventDetail(item.payload),
            data: item.payload,
          };
          return <ActivityCard key={entry.key} activity={activity} />;
        }
        if (item.kind === "plan" || item.kind === "goal") {
          const text = typeof item.payload.text === "string"
            ? item.payload.text
            : typeof item.payload.objective === "string"
              ? item.payload.objective
              : "";
          return (
            <details className="chat-activity-card" key={entry.key} open={streaming && item.lifecycle !== "completed"}>
              <summary>
                <span className="chat-activity-copy"><small>{item.kind === "plan" ? "计划" : "目标"}</small><strong>{text || (item.kind === "plan" ? "正在更新计划" : "正在更新目标")}</strong></span>
                <ChevronDown size={14} className="details-chevron" />
              </summary>
              {text && <pre>{text}</pre>}
            </details>
          );
        }
        return null;
      })}
    </div>
  );
}

/** Pending interactions belong immediately above the blocked composer. */
function ComposerInteractionTray({
  surfaces,
  approvals = [],
  onInteraction,
}: {
  surfaces: A2UISurface[];
  approvals?: Array<{ id: string; title: string; revision: number }>;
  onInteraction: (interactionId: string, revision: number, name: string, data: Record<string, unknown>) => Promise<void>;
}) {
  const pending = surfaces.filter(surface => surface.interaction?.status === "pending");
  if (!pending.length && !approvals.length) return null;
  return (
    <div className="chat-pending-interactions" aria-label="待处理确认" data-ui="interaction-tray">
      <div className="chat-pending-interactions-heading">
        <ShieldAlert size={16} />
        <strong>等待你的确认</strong>
        <span>处理后将继续当前对话</span>
      </div>
      {pending.map(surface => (
        <A2UIRenderer key={surface.id} surface={surface} onSubmit={onInteraction} />
      ))}
      {approvals.map(approval => (
        <div className="chat-activity-card approval" key={approval.id} data-ui="approval-card">
          <div className="chat-activity-row">
            <span className="chat-activity-copy"><small>工具操作</small><strong>{approval.title}</strong></span>
            <span className="chat-run-error-actions">
              <button className="button secondary small" type="button" onClick={() => { void onInteraction(approval.id, approval.revision, "reject", {}); }}>拒绝</button>
              <button className="button primary small" type="button" onClick={() => { void onInteraction(approval.id, approval.revision, "approve", {}); }}>允许</button>
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

function PersistedInteractionTray({
  runId,
  status,
  onInteraction,
}: {
  runId: string;
  status?: string;
  onInteraction: (runId: string, interactionId: string, revision: number, name: string, data: Record<string, unknown>) => Promise<void>;
}) {
  const [events, setEvents] = useState<RunEvent[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/events`);
        if (!response.ok) return;
        const parsed: RunEvent[] = [];
        const parser = createResponseSseParser(event => {
          const { type, ...data } = event;
          parsed.push({ id: parsed.length + 1, type, data });
        });
        parser.push(await response.text());
        parser.finish();
        if (!cancelled) setEvents(parsed);
      } catch {
        // Never invent an action if the enhanced event feed is unavailable.
      }
    }
    void load();
    const timer = status === "WAITING_INPUT" ? window.setInterval(load, 500) : null;
    return () => {
      cancelled = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [runId, status]);

  return (
    <ComposerInteractionTray
      surfaces={projectA2UISurfaces(events)}
      onInteraction={(interactionId, revision, name, data) => onInteraction(runId, interactionId, revision, name, data)}
    />
  );
}

function RunActivityCards({
  runId,
  status,
  durationMs,
  showOutput = false,
  onInteraction,
}: {
  runId: string;
  status?: string;
  durationMs?: number | null;
  showOutput?: boolean;
  onInteraction: (runId: string, interactionId: string, revision: number, name: string, data: Record<string, unknown>) => Promise<void>;
}) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/events`);
        if (!response.ok) throw new Error(await responseError(response));
        const parsed: RunEvent[] = [];
        const parser = createResponseSseParser(event => {
          const { type, ...data } = event;
          parsed.push({ id: parsed.length + 1, type, data });
        });
        parser.push(await response.text());
        parser.finish();
        if (!cancelled) setEvents(parsed);
      } catch {
        // 事件卡片是增强信息；历史正文仍然可以独立展示。
      } finally {
        if (!cancelled) setLoaded(true);
      }
    }
    load();
    const timer = ["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(status)) ? window.setInterval(load, 500) : null;
    return () => {
      cancelled = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [runId, status]);

  const projection = useMemo(() => projectRunActivities(events), [events]);
  const surfaces = useMemo(() => projectA2UISurfaces(events), [events]);
  const inlineSurfaces = surfaces.filter(surface => surface.interaction?.status !== "pending");
  if (!loaded && ["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(status))) {
    return <div className="chat-activity-loading"><Loader2 size={13} className="animate-spin" /> 正在读取运行事件</div>;
  }
  if (!projection.reasoning && !projection.output && projection.activities.length === 0 && surfaces.length === 0) {
    return status === "RUNNING"
      ? <span className="message-loading" aria-label="正在生成"><i /><i /><i /></span>
      : null;
  }
  return (
    <>
      <ProcessingGroup
        reasoning={projection.reasoning}
        activities={projection.activities}
        streaming={status === "RUNNING"}
        durationMs={durationMs}
      />
      {inlineSurfaces.map(surface => (
        <A2UIRenderer
          key={surface.id}
          surface={surface}
          onSubmit={(interactionId, revision, name, data) => onInteraction(runId, interactionId, revision, name, data)}
        />
      ))}
      {showOutput && projection.output && <MarkdownMessage>{projection.output}</MarkdownMessage>}
    </>
  );
}

function MarkdownCodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const child = Children.toArray(children)[0] ?? null;
  const codeProps = isValidElement<{ className?: string; children?: ReactNode }>(child)
    ? child.props
    : {};
  const language = codeProps.className?.replace(/^language-/, "") || "代码";
  const raw = String(codeProps.children ?? "").replace(/\n$/, "");

  async function copyCode() {
    if (!navigator.clipboard || !raw) return;
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="chat-code-block">
      <div className="chat-code-header">
        <span>{language}</span>
        <button type="button" onClick={() => { void copyCode(); }} aria-label={copied ? "已复制代码" : "复制代码"}>
          {copied ? <Check size={13} /> : <Copy size={13} />}
          <span>{copied ? "已复制" : "复制"}</span>
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

function MarkdownMessage({ children, streaming = false }: { children: string; streaming?: boolean }) {
  return (
    <div className={`chat-markdown${streaming ? " streaming" : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: label }) => <a href={href} target="_blank" rel="noreferrer">{label}</a>,
          code: ({ className, children: code }) => <code className={className}>{code}</code>,
          pre: ({ children: code }) => <MarkdownCodeBlock>{code}</MarkdownCodeBlock>,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function PersistedTurn({
  run,
  agentName,
  agentAppearance,
  onInteraction,
  onConfigure,
  onOpenSettings,
  onRetry,
}: {
  run: ChatRun;
  agentName: string;
  agentAppearance?: AgentAppearance;
  onInteraction: (runId: string, interactionId: string, revision: number, name: string, data: Record<string, unknown>) => Promise<void>;
  onConfigure?: () => void;
  onOpenSettings?: () => void;
  onRetry: (prompt: string) => void;
}) {
  const failed = run.status && !["COMPLETED", "RUNNING", "PAUSED", "WAITING_INPUT"].includes(run.status);
  const assistantText = run.output
    || run.error?.message
    || (failed ? `运行状态：${run.status}` : "");
  return (
    <>
      <article className="message user" data-ui="bubble" data-role="user">
        <div className="message-meta"><strong>你</strong><span>{formatMessageTime(run.startedAt)}</span></div>
        <div className="message-content"><span className="plain-message">{run.input}</span></div>
      </article>
      <article className={`message assistant${failed ? " error" : ""}`} data-ui="bubble" data-role="assistant">
        <div className="message-meta">
          <AgentAvatar name={agentName} appearance={agentAppearance} size="xs" />
          <strong>{agentName}</strong>
          <span>{formatMessageTime(run.completedAt || run.startedAt)}</span>
          {run.model && <span className="message-model">{run.model}</span>}
        </div>
        <div className="message-content">
          <RunActivityCards
            runId={run.id}
            status={run.status}
            durationMs={run.durationMs}
            showOutput={["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(run.status))}
            onInteraction={onInteraction}
          />
          {["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(run.status)) ? null : failed ? (
            <RunErrorCard
              error={assistantText}
              onConfigure={onConfigure}
              onOpenSettings={onOpenSettings}
              onRetry={() => onRetry(run.input)}
            />
          ) : assistantText ? (
            <MarkdownMessage>{assistantText}</MarkdownMessage>
          ) : null}
        </div>
      </article>
    </>
  );
}

function StreamingTurn({
  prompt,
  stream,
  agentName,
  agentAppearance,
  onInteraction,
  onConfigure,
  onOpenSettings,
  onRetry,
}: {
  prompt: string;
  stream: ChatStreamState;
  agentName: string;
  agentAppearance?: AgentAppearance;
  onInteraction: (runId: string, interactionId: string, revision: number, name: string, data: Record<string, unknown>) => Promise<void>;
  onConfigure?: () => void;
  onOpenSettings?: () => void;
  onRetry: (prompt: string) => void;
}) {
  const hasTimeline = stream.timeline.length > 0;
  return (
    <>
      <article className="message user" data-ui="bubble" data-role="user">
        <div className="message-meta"><strong>你</strong><span>刚刚</span></div>
        <div className="message-content"><span className="plain-message">{prompt}</span></div>
      </article>
      <article className={`message assistant streaming-turn${stream.status === "failed" ? " error" : ""}`} data-ui="bubble" data-role="assistant">
        <div className="message-meta">
          <AgentAvatar name={agentName} appearance={agentAppearance} size="xs" />
          <strong>{agentName}</strong>
          <span>{stream.status === "streaming" ? "正在生成" : "刚刚"}</span>
        </div>
        <div className="message-content">
          {hasTimeline ? (
            <ConversationTimeline items={stream.timeline} streaming={stream.status === "streaming"} />
          ) : (
            <ProcessingGroup reasoning={stream.reasoning} activities={stream.activities} streaming={stream.status === "streaming"} />
          )}
          {stream.surfaces.filter(surface => surface.interaction?.status !== "pending").map(surface => (
            <A2UIRenderer
              key={surface.id}
              surface={surface}
              onSubmit={(interactionId, revision, name, data) => onInteraction(stream.runId, interactionId, revision, name, data)}
            />
          ))}
          {stream.artifacts.map(artifact => (
            <div className="chat-activity-card artifact" key={artifact.id} data-ui="artifact">
              <div className="chat-activity-row">
                <span className="chat-activity-copy">
                  <small>{artifact.mimeType}</small>
                  <strong>{artifact.name}</strong>
                </span>
                {artifact.uri && <a href={artifact.uri} target="_blank" rel="noreferrer">打开</a>}
              </div>
            </div>
          ))}
          {stream.fallbacks.map(card => (
            <div
              className={`chat-activity-card unknown${card.failed ? " failed" : ""}`}
              key={card.id}
              data-ui="conversation-fallback"
              role={card.failed ? "alert" : "status"}
            >
              <div className="chat-activity-row">
                <span className="chat-activity-copy"><small>{card.title}</small><strong>{card.detail}</strong></span>
              </div>
            </div>
          ))}
          {!hasTimeline && stream.output ? <MarkdownMessage streaming={stream.status === "streaming"}>{stream.output}</MarkdownMessage> : stream.error ? (
            <RunErrorCard
              error={stream.error}
              onConfigure={onConfigure}
              onOpenSettings={onOpenSettings}
              onRetry={() => onRetry(prompt)}
            />
          ) : stream.status === "cancelled" ? (
            <span className="plain-message">运行已停止</span>
          ) : hasTimeline || stream.artifacts.length || stream.fallbacks.length || stream.surfaces.length ? null : (
            <span className="message-loading"><i /><i /><i /></span>
          )}
        </div>
      </article>
    </>
  );
}

export function ChatWorkspace({
  agentId,
  agentName,
  agentAppearance,
  active = true,
  refreshTick = 0,
  onRunChanged,
  onConfigureAgent,
  onOpenSettings,
}: ChatWorkspaceProps) {
  const [runs, setRuns] = useState<ChatRun[]>([]);
  const [models, setModels] = useState<ChatModel[]>([]);
  const [model, setModel] = useState("");
  const [currentSessionId, setCurrentSessionId] = useState("");
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("risk");
  const [collaborationMode, setCollaborationMode] = useState<CollaborationMode>("default");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("");
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [conversationSurface, setConversationSurface] = useState<ConversationSurfaceState>({ status: "loading" });
  const [commandIndex, setCommandIndex] = useState(0);
  const [stream, setStream] = useState<ChatStreamState | null>(null);
  const [optimisticPrompt, setOptimisticPrompt] = useState("");
  const [loading, setLoading] = useState(true);
  const [deleteSessionId, setDeleteSessionId] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const submissionInFlightRef = useRef(false);
  const followBottomRef = useRef(true);
  const scrollBySessionRef = useRef(new Map<string, number>());
  const previewSurfaceSessionId = useMemo(() => uniqueId("ses_surface"), [agentId]);

  const sessions = useMemo(() => groupRunsBySession(runs, agentId), [runs, agentId]);
  const filteredSessions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized
      ? sessions.filter(session => session.title.toLocaleLowerCase().includes(normalized))
      : sessions;
  }, [query, sessions]);
  const currentSession = sessions.find(session => session.id === currentSessionId);
  const visibleRuns = currentSession?.runs || [];
  const persistedActiveRun = latestActiveRun(visibleRuns);
  const activeStatus = stream?.status || String(persistedActiveRun?.status || "").toLowerCase();
  const isGenerating = ["streaming", "paused", "waiting_input"].includes(activeStatus) || Boolean(persistedActiveRun);
  const displayRuns = persistedRunsForDisplay(
    visibleRuns,
    stream && optimisticPrompt && stream.sessionId === currentSessionId
      ? persistedActiveRun?.id
      : undefined,
  );
  const activeStatusLabel = activeStatus === "paused"
    ? "PAUSED"
    : activeStatus === "waiting_input"
      ? "WAITING"
      : "RUNNING";
  const activeMode: RuntimeMode | null = stream?.goalObjective || persistedActiveRun?.goalObjective
    ? "goal"
    : (stream?.collaborationMode || persistedActiveRun?.collaborationMode) === "plan"
      ? "plan"
      : null;
  const activeModeStatus: RuntimeModeStatus = activeStatus === "paused"
    ? "paused"
    : activeStatus === "waiting_input"
      ? "waiting"
      : "running";
  const activeModeObjective = stream?.goalObjective
    || persistedActiveRun?.goalObjective
    || optimisticPrompt
    || persistedActiveRun?.input
    || "";
  const activeModeStartedAt = stream?.startedAt || persistedActiveRun?.startedAt;
  const activeModeElapsedMs = activeStatus === "paused" ? persistedActiveRun?.durationMs : undefined;
  const waitingPersistedRuns = !stream
    ? displayRuns.filter(run => String(run.status) === "WAITING_INPUT")
    : [];
  const selectedModel = models.find(item => item.id === model);
  const streamUsage = stream?.usage as Record<string, number> | undefined;
  const contextUsage = contextUsageState(
    streamUsage?.input_tokens
      ?? streamUsage?.inputTokens
      ?? latestReportedInputTokens(visibleRuns),
    selectedModel?.context_window_tokens ?? selectedModel?.contextWindowTokens,
  );
  const composerModels: ComposerModelOption[] = models.map(item => ({
    id: item.id,
    label: item.display_name || item.displayName || item.id,
    reasoningEfforts: explicitReasoningEfforts(item),
  }));
  const effectiveReasoningEffort = explicitReasoningEfforts(selectedModel).includes(reasoningEffort)
    ? reasoningEffort
    : "";
  const allowsText = surfacePermitsInput(conversationSurface, "text");
  const allowsImageAttachment = surfacePermitsInput(
    conversationSurface,
    "attachment.image",
    "attachments",
  );
  const allowsFileAttachment = surfacePermitsInput(
    conversationSurface,
    "attachment.file",
    "attachments",
  );
  const allowsAttachments = allowsImageAttachment || allowsFileAttachment;
  const allowsPlan = surfacePermitsInput(conversationSurface, "plan");
  const allowsGoal = surfacePermitsInput(conversationSurface, "goal");
  const allowsApproval = surfacePermitsInput(conversationSurface, "approval", "approval.mode");
  const allowsModelSelection = surfacePermitsInput(conversationSurface, "model.select");
  const allowsReasoning = surfacePermitsInput(conversationSurface, "reasoning.effort");
  const surfaceLoading = conversationSurface.status === "loading";
  const attachmentAccept = allowsImageAttachment && allowsFileAttachment
    ? COMPOSER_ATTACHMENT_ACCEPT
    : allowsImageAttachment
      ? "image/*"
      : COMPOSER_ATTACHMENT_ACCEPT.split(",").filter(value => value !== "image/*").join(",");

  useEffect(() => {
    if (reasoningEffort && !explicitReasoningEfforts(selectedModel).includes(reasoningEffort)) {
      setReasoningEffort("");
    }
  }, [reasoningEffort, selectedModel]);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    const surfaceSessionId = currentSessionId || previewSurfaceSessionId;
    setConversationSurface({ status: "loading" });
    const conversationClient = new HttpConversationClient({ fetch: apiFetch });
    conversationClient.getSurface(agentId, surfaceSessionId, { signal: controller.signal })
      .then(({ buildId, surface }) => {
        if (surface.sessionId !== surfaceSessionId) {
          throw new ConversationClientError(
            "conversation_session_mismatch",
            "Conversation surface changed session identity.",
          );
        }
        if (!cancelled) setConversationSurface({
          status: "declared",
          buildId,
          surface,
        });
      })
      .catch(error => {
        if (cancelled
          || (error instanceof Error && error.name === "AbortError")
          || (error instanceof ConversationClientError && error.code === "conversation_aborted")) return;
        // Only an absent endpoint identifies a 0.8.2 legacy Studio.  A
        // network/build/server failure must fail closed instead of exposing
        // controls whose Runtime support could not be proven.
        if (error instanceof ConversationClientError
          && error.code === "conversation_http_error"
          && error.status === 404) {
          setConversationSurface({ status: "legacy" });
          return;
        }
        setConversationSurface({ status: "error" });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [agentId, currentSessionId, previewSurfaceSessionId, refreshTick]);

  useEffect(() => {
    if (conversationSurface.status !== "declared") return;
    if (!allowsPlan && collaborationMode === "plan") {
      setCollaborationMode("default");
      localStorage.setItem(`agentkit:chat:collaboration:${agentId}`, "default");
    }
    if (!allowsAttachments && attachments.length) setAttachments([]);
    if (!allowsReasoning && reasoningEffort) setReasoningEffort("");
  }, [
    agentId,
    allowsAttachments,
    allowsPlan,
    allowsReasoning,
    attachments.length,
    collaborationMode,
    conversationSurface.status,
    reasoningEffort,
  ]);

  const refreshRuns = useCallback(async () => {
    const runResponse = await apiFetch("/api/v1/runs");
    if (!runResponse.ok) throw new Error(await responseError(runResponse));
    const runPayload = await runResponse.json();
    const nextRuns: ChatRun[] = runPayload.items || [];
    const nextSessions = groupRunsBySession(nextRuns, agentId);
    setRuns(nextRuns);
    setCurrentSessionId(previous => (
      previous && nextSessions.some(session => session.id === previous)
        ? previous
        : nextSessions[0]?.id || ""
    ));
  }, [agentId]);

  useEffect(() => {
    setSessionPanelOpen(false);
  }, [agentId]);

  const loadWorkspace = useCallback(async () => {
    const [, modelResponse] = await Promise.all([
      refreshRuns(),
      apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/models`),
    ]);
    if (!modelResponse.ok) throw new Error(await responseError(modelResponse));
    const modelPayload = await modelResponse.json();
    const nextModels: ChatModel[] = modelPayload.Models || [];
    setModels(nextModels);
    setModel(previous => {
      if (nextModels.some(item => item.id === previous)) return previous;
      return String(modelPayload.Current || nextModels[0]?.id || "");
    });
  }, [agentId, refreshRuns]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setRuns([]);
    setCurrentSessionId("");
    loadWorkspace()
      .catch(error => { if (!cancelled) showToast("会话加载失败", error.message, "error"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => {
      cancelled = true;
      abortRef.current?.abort();
    };
  }, [agentId, loadWorkspace, refreshTick]); // Agent/Build 变更后同步最新模型锁定；运行中的刷新由下方轮询负责。

  useEffect(() => {
    setApprovalMode(normalizeApprovalMode(localStorage.getItem(approvalModeStorageKey(agentId))));
    const storedMode = localStorage.getItem(`agentkit:chat:collaboration:${agentId}`);
    setCollaborationMode(storedMode === "plan" ? "plan" : "default");
    setReasoningEffort("");
    setAttachments([]);
  }, [agentId]);

  useEffect(() => {
    setCommandIndex(0);
  }, [input]);

  useEffect(() => {
    if (!runs.some(run => ["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(run.status)))) return;
    const timer = window.setInterval(() => { refreshRuns().catch(() => {}); }, 800);
    return () => window.clearInterval(timer);
  }, [runs, refreshRuns]);

  useEffect(() => {
    const list = messageListRef.current;
    if (!list || !followBottomRef.current) return;
    list.scrollTop = list.scrollHeight;
  }, [visibleRuns.length, stream?.output, stream?.reasoning, stream?.status, stream?.activities]);

  useEffect(() => {
    const list = messageListRef.current;
    if (!list) return;
    const saved = currentSessionId ? scrollBySessionRef.current.get(currentSessionId) : undefined;
    requestAnimationFrame(() => {
      list.scrollTop = saved ?? list.scrollHeight;
      followBottomRef.current = saved === undefined || list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    });
  }, [currentSessionId]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "42px";
    textarea.style.height = `${Math.min(Math.max(textarea.scrollHeight, 42), 160)}px`;
  }, [input]);

  function startNewSession() {
    if (isGenerating) return;
    setCurrentSessionId("");
    setOptimisticPrompt("");
    setStream(null);
    setInput("");
    setAttachments([]);
    setSessionPanelOpen(false);
    followBottomRef.current = true;
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function selectSession(sessionId: string) {
    const list = messageListRef.current;
    if (list && currentSessionId) scrollBySessionRef.current.set(currentSessionId, list.scrollTop);
    setCurrentSessionId(sessionId);
    setSessionPanelOpen(false);
    followBottomRef.current = !scrollBySessionRef.current.has(sessionId);
  }

  function changeCollaborationMode(next: CollaborationMode) {
    setCollaborationMode(next);
    localStorage.setItem(`agentkit:chat:collaboration:${agentId}`, next);
  }

  function togglePlanMode() {
    const next = collaborationMode === "plan" ? "default" : "plan";
    changeCollaborationMode(next);
    setInput("");
    showToast(next === "plan" ? "计划模式已开启" : "已返回默认模式", "下一轮对话生效", "success");
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function selectComposerCommand(id: ComposerCommand["id"]) {
    if (id === "goal") {
      setInput("/goal ");
      requestAnimationFrame(() => textareaRef.current?.focus());
      return;
    }
    if (id === "default") {
      changeCollaborationMode("default");
      setInput("");
      showToast("已返回默认模式", "下一轮对话生效", "success");
      return;
    }
    togglePlanMode();
  }

  async function addAttachments(files: File[]) {
    if (isGenerating) return;
    if (!allowsAttachments) {
      showToast("当前 Agent 不支持附件", "运行时没有声明附件输入能力。", "error");
      return;
    }
    const supportedFiles = files.filter(file => (
      file.type.startsWith("image/") ? allowsImageAttachment : allowsFileAttachment
    ));
    if (supportedFiles.length !== files.length) {
      showToast("部分附件未添加", "当前 Agent 没有声明对应的图片或文件输入能力。", "error");
    }
    if (!supportedFiles.length) return;
    const available = Math.max(0, MAX_COMPOSER_ATTACHMENTS - attachments.length);
    if (!available) {
      showToast("附件数量已达上限", `每轮最多 ${MAX_COMPOSER_ATTACHMENTS} 个`, "error");
      return;
    }
    const selected = supportedFiles.slice(0, available);
    const existingBytes = attachments.reduce((total, item) => total + item.size, 0);
    if (existingBytes + selected.reduce((total, file) => total + file.size, 0) > MAX_COMPOSER_ATTACHMENT_BYTES) {
      showToast("附件体积过大", "每轮附件总计不能超过 1.5 MiB", "error");
      return;
    }
    const next: ComposerAttachment[] = [];
    for (const file of selected) {
      try {
        next.push(await fileToComposerAttachment(file));
      } catch (error) {
        showToast("无法添加附件", error instanceof Error ? error.message : String(error), "error");
      }
    }
    if (!next.length) return;
    const combined = [...attachments, ...next];
    if (encodedComposerAttachmentsBytes(combined) > MAX_COMPOSER_ATTACHMENT_BYTES) {
      showToast("附件编码后体积过大", "每轮编码后的附件总计不能超过 1.5 MiB", "error");
      return;
    }
    setAttachments(combined);
  }

  async function sendMessage(inputOverride?: string) {
    const retrying = typeof inputOverride === "string";
    const sourceInput = retrying ? inputOverride : input;
    const turnSourceAttachments = retrying ? [] : attachments;
    const submission = parseComposerSubmission(sourceInput);
    if (isGenerating || submissionInFlightRef.current) return;
    if (surfaceLoading) {
      showToast("正在确认会话能力", "请稍后再发送。", "error");
      return;
    }
    if (!allowsText) {
      showToast("当前 Agent 不支持文字会话", "运行时没有声明文字输入能力。", "error");
      return;
    }
    if (!model) {
      showToast("当前 Agent 尚未绑定模型", "请先完成模型绑定和凭证配置。", "error");
      onConfigureAgent?.();
      return;
    }
    if (submission.kind === "toggle-plan") {
      if (!allowsPlan) {
        showToast("当前 Agent 不支持计划模式", "运行时没有声明 Plan 输入能力。", "error");
        return;
      }
      togglePlanMode();
      return;
    }
    if (submission.kind === "set-default") {
      changeCollaborationMode("default");
      setInput("");
      showToast("已返回默认模式", "下一轮对话生效", "success");
      return;
    }
    if (submission.kind === "goal" && !allowsGoal) {
      showToast("当前 Agent 不支持长期目标", "运行时没有声明 Goal 输入能力。", "error");
      return;
    }
    if (submission.kind === "goal" && !submission.objective) {
      showToast("请补充目标", "在 /goal 后输入需要持续完成的目标", "error");
      requestAnimationFrame(() => textareaRef.current?.focus());
      return;
    }
    const goalObjective = submission.kind === "goal" ? submission.objective : "";
    const content = submission.kind === "message"
      ? submission.text || (turnSourceAttachments.length ? "请分析这些附件。" : "")
      : goalObjective;
    if (turnSourceAttachments.length && !allowsAttachments) {
      showToast("当前 Agent 不支持附件", "请移除附件后重试。", "error");
      return;
    }
    if (!content && !turnSourceAttachments.length) return;
    submissionInFlightRef.current = true;
    const sessionId = currentSessionId
      || (conversationSurface.status === "declared"
        ? conversationSurface.surface.sessionId
        : uniqueId("ses"));
    const invocationId = uniqueId("resp");
    const approvalModeForTurn = approvalMode;
    const controller = new AbortController();
    abortRef.current = controller;
    let aggregate: ChatStreamState = {
      ...createChatStreamState(invocationId, sessionId),
      transport: conversationSurface.status === "declared" ? "conversation" : "responses",
      collaborationMode,
      goalObjective,
      startedAt: new Date().toISOString(),
    };
    setCurrentSessionId(sessionId);
    setOptimisticPrompt(content);
    setStream(aggregate);
    if (!retrying) setInput("");
    const turnAttachments = turnSourceAttachments;
    if (!retrying) setAttachments([]);

    try {
      const applyStreamEvent = (event: Parameters<typeof reduceChatStreamEvent>[1]) => {
        aggregate = reduceChatStreamEvent(aggregate, event);
        setStream(aggregate);
      };
      if (conversationSurface.status === "declared") {
        const uploaded = await Promise.all(
          turnAttachments.map(item => uploadConversationAttachment(item, controller.signal)),
        );
        const conversationInput = buildConversationInput({
          inputId: invocationId,
          sessionId,
          idempotencyKey: invocationId,
          parts: [
            { kind: "text", text: content },
            ...uploaded.map(item => ({
              kind: "attachment" as const,
              attachmentRef: item.attachmentRef,
              mediaType: item.mediaType,
              name: item.name,
            })),
          ],
          ...(allowsModelSelection ? { modelRef: model } : {}),
          ...(allowsReasoning && effectiveReasoningEffort ? { reasoning: effectiveReasoningEffort } : {}),
          extensions: {
            ...(allowsApproval ? { "ksadk.approval": approvalModeForTurn } : {}),
            ...(allowsPlan ? { "ksadk.collaboration": collaborationMode } : {}),
            ...(allowsGoal && goalObjective ? { "ksadk.goal": goalObjective } : {}),
          },
        });
        const conversationClient = new HttpConversationClient({ fetch: apiFetch });
        await conversationClient.streamTurn({
          bootstrap: {
            buildId: conversationSurface.buildId,
            surface: conversationSurface.surface,
          },
          input: conversationInput,
          signal: controller.signal,
          onUpdate: result => {
            aggregate = applyConversationStreamResult(aggregate, result);
            setStream(aggregate);
          },
        });
      } else {
        // A 404 Surface endpoint is the only legacy signal. Existing 0.8.2
        // agents keep the exact Responses wire while current agents use the
        // typed ConversationInput path above.
        const response = await apiFetch("/v1/responses", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          signal: controller.signal,
          body: JSON.stringify({
            ...(allowsModelSelection ? { model } : {}),
            ...(allowsReasoning && effectiveReasoningEffort ? { reasoning: { effort: effectiveReasoningEffort } } : {}),
            input: buildResponsesInput(content, turnAttachments),
            stream: true,
            metadata: {
              agent_id: agentId,
              session_id: sessionId,
              invocation_id: invocationId,
              ...(allowsApproval ? { approval_mode: approvalModeForTurn } : {}),
              ...(allowsPlan ? { collaboration_mode: collaborationMode } : {}),
              ...(allowsGoal && goalObjective ? { goal_objective: goalObjective } : {}),
            },
          }),
        });
        if (!response.ok || !response.body) throw new Error(await responseError(response));
        // The legacy Responses transport intentionally retains its exact 0.8.2
        // one-shot behavior. Conversation RunEvent cursors are not inferred
        // for agents that did not declare a typed Surface.
        const decoder = new TextDecoder();
        const parser = createResponseSseParser(applyStreamEvent);
        const reader = response.body.getReader();
        while (true) {
          const { value, done } = await reader.read();
          if (value) parser.push(decoder.decode(value, { stream: !done }));
          if (done) break;
        }
        parser.finish();
      }
      if (aggregate.status === "failed") throw new Error(aggregate.error || "Agent 运行失败");
      await refreshRuns();
      setStream(null);
      setOptimisticPrompt("");
      onRunChanged?.();
    } catch (error) {
      if (controller.signal.aborted) {
        aggregate = { ...aggregate, status: "cancelled" };
        setStream(aggregate);
      } else {
        const message = conversationFailureMessage(error);
        aggregate = { ...aggregate, status: "failed", error: message };
        setStream(aggregate);
        showToast("运行失败", message, "error");
      }
    } finally {
      abortRef.current = null;
      submissionInFlightRef.current = false;
      requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }

  function changeApprovalMode(next: ApprovalMode) {
    setApprovalMode(next);
    localStorage.setItem(approvalModeStorageKey(agentId), next);
  }

  async function pauseResponse() {
    if (!isGenerating) return;
    try {
      const response = stream?.transport === "conversation" && stream.runId
        ? await apiFetch(`/api/v1/runs/${encodeURIComponent(stream.runId)}:pause`, { method: "POST" })
        : stream?.status === "streaming"
        ? await apiFetch(`/v1/responses/${encodeURIComponent(stream.responseId)}:pause`, {
            method: "POST",
            credentials: "same-origin",
          })
        : await apiFetch(`/api/v1/runs/${encodeURIComponent(persistedActiveRun!.id)}:pause`, { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response));
      setStream(previous => previous ? { ...previous, status: "paused" } : previous);
      window.setTimeout(() => { refreshRuns().catch(() => {}); }, 200);
    } catch (error) {
      showToast("暂停运行失败", error instanceof Error ? error.message : String(error), "error");
    }
  }

  async function resumeResponse() {
    try {
      const response = stream?.transport === "conversation" && stream.runId
        ? await apiFetch(`/api/v1/runs/${encodeURIComponent(stream.runId)}:resume`, { method: "POST" })
        : stream?.status === "paused"
        ? await apiFetch(`/v1/responses/${encodeURIComponent(stream.responseId)}:resume`, {
            method: "POST",
            credentials: "same-origin",
          })
        : await apiFetch(`/api/v1/runs/${encodeURIComponent(persistedActiveRun!.id)}:resume`, { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response));
      setStream(previous => previous ? { ...previous, status: "streaming" } : previous);
      window.setTimeout(() => { refreshRuns().catch(() => {}); }, 200);
    } catch (error) {
      showToast("继续运行失败", error instanceof Error ? error.message : String(error), "error");
    }
  }

  async function cancelResponse() {
    if (!isGenerating) return;
    try {
      const response = stream?.transport === "conversation" && stream.runId
        ? await apiFetch(`/api/v1/runs/${encodeURIComponent(stream.runId)}:cancel`, { method: "POST" })
        : stream
        ? await apiFetch(`/v1/responses/${encodeURIComponent(stream.responseId)}/cancel`, {
            method: "POST",
            credentials: "same-origin",
          })
        : await apiFetch(`/api/v1/runs/${encodeURIComponent(persistedActiveRun!.id)}:cancel`, { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response));
      abortRef.current?.abort();
      setStream(previous => previous ? { ...previous, status: "cancelled" } : previous);
      window.setTimeout(() => { refreshRuns().catch(() => {}); }, 200);
    } catch (error) {
      showToast("结束运行失败", error instanceof Error ? error.message : String(error), "error");
    }
  }

  async function submitInteraction(
    runId: string,
    interactionId: string,
    revision: number,
    name: string,
    data: Record<string, unknown>,
  ) {
    if (!runId) throw new Error("运行尚未创建，请稍后重试");
    const response = await apiFetch(
      `/api/v1/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}:submit`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          data,
          expectedRevision: revision,
          idempotencyKey: `interaction:${interactionId}:revision-${revision}`,
        }),
      },
    );
    if (!response.ok) {
      const message = await responseError(response);
      showToast("提交交互失败", message, "error");
      throw new Error(message);
    }
    setStream(previous => previous ? {
      ...previous,
      status: "streaming",
      pendingApprovals: previous.pendingApprovals.filter(item => item.id !== interactionId),
      surfaces: previous.surfaces.map(surface => surface.interaction?.id === interactionId
        ? { ...surface, interaction: { ...surface.interaction, status: "resolved" } }
        : surface),
    } : previous);
    await refreshRuns();
  }

  async function confirmDeleteSession() {
    if (!deleteSessionId) return;
    setDeleting(true);
    try {
      const response = await apiFetch(`/api/v1/sessions/${encodeURIComponent(deleteSessionId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));
      if (currentSessionId === deleteSessionId) setCurrentSessionId("");
      setDeleteSessionId("");
      await refreshRuns();
      showToast("会话已删除", "相关运行与 Trace 已从本地工作区移除。");
    } catch (error) {
      showToast("删除失败", error instanceof Error ? error.message : String(error), "error");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className={`studio-chat-shell${sessionPanelOpen ? " sessions-open" : ""}`} data-testid="studio-chat-workbench">
      <aside className="chat-session-sidebar" aria-label="会话历史">
        <header className="chat-session-header">
          <h2>会话</h2>
          <div className="chat-session-header-actions">
            <button className="icon-button tertiary" type="button" aria-label="新对话" title="新对话" onClick={startNewSession} disabled={isGenerating}>
              <MessageSquarePlus size={16} />
            </button>
            <button className="icon-button tertiary chat-session-mobile-close" type="button" aria-label="关闭会话历史" title="关闭会话历史" onClick={() => setSessionPanelOpen(false)}>
              <X size={17} />
            </button>
          </div>
        </header>
        <label className="chat-session-search">
          <span className="sr-only">搜索会话</span>
          <input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索会话" />
        </label>
        <div className="chat-session-list">
          {loading ? (
            <div className="chat-session-skeleton" aria-label="正在加载会话" role="status">
              <i /><i /><i />
            </div>
          ) : filteredSessions.length === 0 ? (
            <div className="session-empty">{query ? "没有匹配的会话" : "还没有会话"}</div>
          ) : filteredSessions.map(session => (
            <div key={session.id} className={`chat-session-item${currentSessionId === session.id ? " active" : ""}${session.running ? " running" : ""}`}>
              <button
                className="chat-session-main"
                type="button"
                aria-current={currentSessionId === session.id ? "true" : undefined}
                onClick={() => selectSession(session.id)}
                title={`${session.title} · ${formatSessionTime(session.updatedAt)}`}
              >
                <strong>{shortText(session.title)}</strong>
                {session.running && (
                  <span
                    className={`session-status ${String(session.activeStatus || "RUNNING").toLowerCase()}`}
                    aria-label={session.activeStatus === "PAUSED"
                      ? "已暂停"
                      : session.activeStatus === "WAITING_INPUT"
                        ? "等待输入"
                        : "运行中"}
                  />
                )}
              </button>
              <button
                className="chat-session-delete"
                type="button"
                aria-label={`删除会话：${shortText(session.title)}`}
                title={session.running || (stream?.status === "streaming" && stream.sessionId === session.id) ? "运行中不可删除" : "删除会话"}
                disabled={session.running || (stream?.status === "streaming" && stream.sessionId === session.id)}
                onClick={() => setDeleteSessionId(session.id)}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <button
        className="chat-session-backdrop"
        type="button"
        aria-label="关闭会话历史"
        onClick={() => setSessionPanelOpen(false)}
      />

      <section className="chat-conversation" aria-label={`与 ${agentName} 对话`}>
        <header className="chat-conversation-header">
          <button
            className="icon-button tertiary chat-session-mobile-trigger"
            type="button"
            aria-label="打开会话历史"
            title="会话历史"
            aria-expanded={sessionPanelOpen}
            onClick={() => setSessionPanelOpen(true)}
          >
            <PanelLeftOpen size={17} />
          </button>
          <AgentAvatar name={agentName} appearance={agentAppearance} size="sm" />
          <h1>{agentName}</h1>
          {isGenerating && <span className="badge" data-state={activeStatus === "streaming" ? "running" : "pending"}>{activeStatusLabel}</span>}
        </header>

        <div
          ref={messageListRef}
          className="chat-message-list"
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
          aria-busy={isGenerating}
          onScroll={event => {
            const element = event.currentTarget;
            followBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
            if (currentSessionId) scrollBySessionRef.current.set(currentSessionId, element.scrollTop);
          }}
        >
          {visibleRuns.length === 0 && !stream ? (
            <div className="chat-empty">
              <span className="chat-empty-icon"><Bot size={22} /></span>
              <h2>开始与 {agentName} 对话</h2>
              <p>消息通过统一的 Responses API 发送；思考、工具调用和结果会在同一条时间线中呈现。</p>
              <div className="suggestion-list">
                {["先介绍你的职责、能力和工作边界。", "根据当前上下文给出一个清晰的执行计划。", "列出完成任务还需要我提供的信息。"].map(suggestion => (
                  <button key={suggestion} type="button" onClick={() => { setInput(suggestion); textareaRef.current?.focus(); }}>{suggestion}</button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {displayRuns.map(run => (
                <PersistedTurn
                  key={run.id}
                  run={run}
                  agentName={agentName}
                  agentAppearance={agentAppearance}
                  onInteraction={submitInteraction}
                  onConfigure={onConfigureAgent}
                  onOpenSettings={onOpenSettings}
                  onRetry={prompt => { void sendMessage(prompt); }}
                />
              ))}
              {stream && stream.sessionId === currentSessionId && optimisticPrompt && (
                <StreamingTurn
                  prompt={optimisticPrompt}
                  stream={stream}
                  agentName={agentName}
                  agentAppearance={agentAppearance}
                  onInteraction={submitInteraction}
                  onConfigure={onConfigureAgent}
                  onOpenSettings={onOpenSettings}
                  onRetry={prompt => { void sendMessage(prompt); }}
                />
              )}
            </>
          )}
        </div>

        <footer className="chat-composer-wrap">
          {stream && stream.sessionId === currentSessionId ? (
            <ComposerInteractionTray
              surfaces={stream.surfaces}
              approvals={stream.pendingApprovals}
              onInteraction={(interactionId, revision, name, data) => submitInteraction(stream.runId, interactionId, revision, name, data)}
            />
          ) : waitingPersistedRuns.map(run => (
            <PersistedInteractionTray
              key={run.id}
              runId={run.id}
              status={run.status}
              onInteraction={submitInteraction}
            />
          ))}
          {isGenerating && activeMode && (
            <RuntimeModeBar
              mode={activeMode}
              status={activeModeStatus}
              objective={activeModeObjective}
              startedAt={activeModeStartedAt}
              elapsedMs={activeModeElapsedMs}
              onPause={activeModeStatus === "running" ? pauseResponse : undefined}
              onResume={activeModeStatus === "paused" ? resumeResponse : undefined}
              onStop={cancelResponse}
            />
          )}
          <ChatComposer
            input={input}
            placeholder={surfaceLoading
              ? "正在确认会话能力…"
              : conversationSurface.status === "error"
                ? "会话能力加载失败，请刷新后重试"
                : !allowsText
                  ? "当前 Agent 未开放文字输入"
                  : collaborationMode === "plan"
                    ? "描述需要规划的任务…"
                    : "输入消息，或输入 / 使用命令…"}
            disabled={isGenerating || surfaceLoading || !allowsText}
            active={active}
            attachments={(allowsAttachments ? attachments : []).map(attachment => ({
              id: attachment.id,
              name: attachment.name,
              kind: attachment.kind,
              size: attachment.size,
              previewUrl: attachment.dataUrl,
            }))}
            mode={collaborationMode}
            approvalMode={approvalMode}
            models={composerModels}
            model={model}
            reasoningEffort={reasoningEffort}
            commandIndex={commandIndex}
            contextControl={<ContextRing {...contextUsage} />}
            sendControl={activeStatus === "paused" ? (
                <button className="chat-send-button resume" type="button" aria-label="继续生成" title="继续生成" onClick={resumeResponse}><Play size={15} fill="currentColor" /></button>
              ) : activeStatus === "waiting_input" ? (
                <button className="chat-send-button pause" type="button" aria-label="等待交互输入" title="请先处理上方交互卡片" disabled><Loader2 size={15} className="animate-spin" /></button>
              ) : isGenerating ? (
                <button className="chat-send-button pause" type="button" aria-label="暂停生成" title="暂停生成" onClick={pauseResponse}><Pause size={15} fill="currentColor" /></button>
              ) : (
                <button className="chat-send-button" type="button" aria-label="发送消息" title="发送消息" onClick={() => { void sendMessage(); }} disabled={surfaceLoading || !allowsText || (!input.trim() && !(allowsAttachments && attachments.length))}><Send size={15} /></button>
              )}
            canSend={allowsText && Boolean(input.trim() || (allowsAttachments && attachments.length))}
            textareaRef={textareaRef}
            onInputChange={setInput}
            onFiles={addAttachments}
            onRemoveAttachment={id => setAttachments(current => current.filter(item => item.id !== id))}
            onSetMode={next => {
              if (next !== collaborationMode) togglePlanMode();
            }}
            onStartGoal={() => selectComposerCommand("goal")}
            onApprovalModeChange={changeApprovalMode}
            onModelChange={setModel}
            onReasoningEffortChange={setReasoningEffort}
            onConfigureModel={onConfigureAgent}
            onCommandSelect={selectComposerCommand}
            onCommandIndexChange={setCommandIndex}
            onSend={() => { void sendMessage(); }}
            allowAttachments={allowsAttachments}
            allowPlan={allowsPlan}
            allowGoal={allowsGoal}
            allowApproval={allowsApproval}
            allowModelSelection={allowsModelSelection}
            allowReasoning={allowsReasoning}
            attachmentAccept={attachmentAccept}
          />
          <p className="chat-composer-disclaimer">AI 生成内容可能不准确，请核对关键结论与工具操作。</p>
        </footer>
      </section>

      {deleteSessionId && (
        <ConfirmDialog
          title="删除这个会话？"
          description="相关 Run 与 Trace 会从当前本地工作区移除。"
          confirmText="删除会话"
          busy={deleting}
          onConfirm={confirmDeleteSession}
          onCancel={() => setDeleteSessionId("")}
        />
      )}
    </div>
  );
}
