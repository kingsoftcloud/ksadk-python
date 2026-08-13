import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bot,
  BrainCircuit,
  Check,
  ChevronDown,
  FileText,
  Hand,
  ListTodo,
  Loader2,
  MessageSquarePlus,
  Pause,
  Play,
  Send,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { apiFetch } from "../api";
import {
  APPROVAL_MODES,
  approvalModeOption,
  approvalModeStorageKey,
  normalizeApprovalMode,
  type ApprovalMode,
} from "../approvalModes";
import {
  createChatStreamState,
  createResponseSseParser,
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
  type RunActivity,
  type RunEvent,
} from "../chatProtocol";
import { A2UIRenderer } from "./A2UIRenderer";
import { AgentAvatar, type AgentAppearance } from "./AgentAvatar";
import { ConfirmDialog } from "./ConfirmDialog";
import { showToast } from "./Toast";
import {
  buildResponsesInput,
  encodedComposerAttachmentsBytes,
  fileToComposerAttachment,
  formatAttachmentSize,
  MAX_COMPOSER_ATTACHMENT_BYTES,
  MAX_COMPOSER_ATTACHMENTS,
  parseComposerSubmission,
  visibleComposerCommands,
  type CollaborationMode,
  type ComposerAttachment,
  type ComposerCommand,
} from "../composerActions";
import { ComposerActionMenu, ComposerCommandMenu } from "./ComposerActionMenu";
import { RuntimeModeBar, type RuntimeMode, type RuntimeModeStatus } from "./RuntimeModeBar";

interface ChatModel {
  id: string;
  display_name?: string;
  displayName?: string;
  context_window_tokens?: number;
  contextWindowTokens?: number;
}

interface ChatWorkspaceProps {
  agentId: string;
  agentName: string;
  agentAppearance?: AgentAppearance;
  onRunChanged?: () => void;
}

function ApprovalModeMenu({
  value,
  onChange,
}: {
  value: ApprovalMode;
  onChange: (value: ApprovalMode) => void;
}) {
  const selected = approvalModeOption(value);
  const icon = value === "ask"
    ? <Hand size={15} />
    : value === "full"
      ? <ShieldAlert size={15} />
      : <ShieldCheck size={15} />;
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          className={`chat-approval-trigger ${value}`}
          type="button"
          aria-label={`批准模式：${selected.label}`}
          title={`${selected.label}；下一轮生效`}
        >
          {icon}
          <span>{selected.compactLabel}</span>
          <ChevronDown size={13} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="chat-approval-menu"
          side="top"
          sideOffset={9}
          align="start"
          collisionPadding={12}
        >
          <div className="chat-approval-menu-heading">
            <strong>如何批准 Agent 操作？</strong>
            <span>下一轮生效</span>
          </div>
          <DropdownMenu.RadioGroup
            value={value}
            onValueChange={next => onChange(normalizeApprovalMode(next))}
          >
            {APPROVAL_MODES.map(option => (
              <DropdownMenu.RadioItem
                key={option.value}
                value={option.value}
                className={`chat-approval-option ${option.value}`}
              >
                <span className="chat-approval-option-icon">
                  {option.value === "ask" ? <Hand size={17} />
                    : option.value === "full" ? <ShieldAlert size={17} />
                      : <ShieldCheck size={17} />}
                </span>
                <span className="chat-approval-option-copy">
                  <strong>{option.label}</strong>
                  <small>{option.description}</small>
                </span>
                <DropdownMenu.ItemIndicator className="chat-approval-indicator">
                  <Check size={16} />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
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

function ModelMenu({
  models,
  value,
  disabled,
  onChange,
}: {
  models: ChatModel[];
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const selected = models.find(item => item.id === value);
  const label = selected?.display_name || selected?.displayName || selected?.id || "默认模型";
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          className="chat-model-trigger"
          type="button"
          disabled={disabled}
          aria-label={`选择模型，当前 ${label}`}
          title="切换模型；下一轮生效"
        >
          <span>{label}</span>
          <ChevronDown size={13} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="chat-model-menu"
          side="top"
          sideOffset={9}
          align="end"
          collisionPadding={12}
        >
          <div className="chat-model-menu-heading">选择下一轮使用的模型</div>
          <DropdownMenu.RadioGroup value={value} onValueChange={onChange}>
            {models.map(item => {
              const itemLabel = item.display_name || item.displayName || item.id;
              return (
                <DropdownMenu.RadioItem key={item.id} value={item.id} className="chat-model-option">
                  <span>{itemLabel}</span>
                  <DropdownMenu.ItemIndicator><Check size={15} /></DropdownMenu.ItemIndicator>
                </DropdownMenu.RadioItem>
              );
            })}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
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

async function responseError(response: Response): Promise<string> {
  const payload = await response.clone().json().catch(() => null);
  return payload?.error?.message
    || payload?.Message
    || payload?.message
    || `请求失败（HTTP ${response.status}）`;
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
}: {
  reasoning: string;
  activities: RunActivity[];
  streaming?: boolean;
}) {
  if (!reasoning && activities.length === 0) return null;
  const running = activities.find(activity => activity.status === "running" || activity.status === "waiting");
  const title = streaming
    ? running ? `${running.status === "waiting" ? "等待确认" : "正在处理"} · ${running.title}`
      : reasoningPreview(reasoning) ? `正在思考 · ${reasoningPreview(reasoning)}`
        : "正在思考"
    : activities.length > 0 ? `已处理 ${activities.length} 次工具调用`
      : "查看思考过程";
  return (
    <details className="chat-processing-group">
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

function RunActivityCards({
  runId,
  status,
  showOutput = false,
  onInteraction,
}: {
  runId: string;
  status?: string;
  showOutput?: boolean;
  onInteraction: (runId: string, interactionId: string, name: string, data: Record<string, unknown>) => Promise<void>;
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
      <ProcessingGroup reasoning={projection.reasoning} activities={projection.activities} streaming={status === "RUNNING"} />
      {surfaces.map(surface => (
        <A2UIRenderer
          key={surface.id}
          surface={surface}
          onSubmit={(interactionId, name, data) => onInteraction(runId, interactionId, name, data)}
        />
      ))}
      {showOutput && projection.output && <MarkdownMessage>{projection.output}</MarkdownMessage>}
    </>
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
}: {
  run: ChatRun;
  agentName: string;
  agentAppearance?: AgentAppearance;
  onInteraction: (runId: string, interactionId: string, name: string, data: Record<string, unknown>) => Promise<void>;
}) {
  const failed = run.status && !["COMPLETED", "RUNNING", "PAUSED", "WAITING_INPUT"].includes(run.status);
  const assistantText = run.output
    || run.error?.message
    || (failed ? `运行状态：${run.status}` : "");
  return (
    <>
      <article className="message user">
        <div className="message-meta"><strong>你</strong><span>{formatMessageTime(run.startedAt)}</span></div>
        <div className="message-content"><span className="plain-message">{run.input}</span></div>
      </article>
      <article className={`message assistant${failed ? " error" : ""}`}>
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
            showOutput={["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(run.status))}
            onInteraction={onInteraction}
          />
          {["RUNNING", "PAUSED", "WAITING_INPUT"].includes(String(run.status)) ? null : failed ? (
            <span className="plain-message">{assistantText}</span>
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
}: {
  prompt: string;
  stream: ChatStreamState;
  agentName: string;
  agentAppearance?: AgentAppearance;
  onInteraction: (runId: string, interactionId: string, name: string, data: Record<string, unknown>) => Promise<void>;
}) {
  return (
    <>
      <article className="message user">
        <div className="message-meta"><strong>你</strong><span>刚刚</span></div>
        <div className="message-content"><span className="plain-message">{prompt}</span></div>
      </article>
      <article className={`message assistant streaming-turn${stream.status === "failed" ? " error" : ""}`}>
        <div className="message-meta">
          <AgentAvatar name={agentName} appearance={agentAppearance} size="xs" />
          <strong>{agentName}</strong>
          <span>{stream.status === "streaming" ? "正在生成" : "刚刚"}</span>
        </div>
        <div className="message-content">
          <ProcessingGroup reasoning={stream.reasoning} activities={stream.activities} streaming={stream.status === "streaming"} />
          {stream.surfaces.map(surface => (
            <A2UIRenderer
              key={surface.id}
              surface={surface}
              onSubmit={(interactionId, name, data) => onInteraction(stream.runId, interactionId, name, data)}
            />
          ))}
          {stream.output ? <MarkdownMessage streaming={stream.status === "streaming"}>{stream.output}</MarkdownMessage> : stream.error ? (
            <span className="plain-message">{stream.error}</span>
          ) : stream.status === "cancelled" ? (
            <span className="plain-message">运行已停止</span>
          ) : (
            <span className="message-loading"><i /><i /><i /></span>
          )}
        </div>
      </article>
    </>
  );
}

export function ChatWorkspace({ agentId, agentName, agentAppearance, onRunChanged }: ChatWorkspaceProps) {
  const [runs, setRuns] = useState<ChatRun[]>([]);
  const [models, setModels] = useState<ChatModel[]>([]);
  const [model, setModel] = useState("");
  const [currentSessionId, setCurrentSessionId] = useState("");
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("risk");
  const [collaborationMode, setCollaborationMode] = useState<CollaborationMode>("default");
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [commandIndex, setCommandIndex] = useState(0);
  const [stream, setStream] = useState<ChatStreamState | null>(null);
  const [optimisticPrompt, setOptimisticPrompt] = useState("");
  const [loading, setLoading] = useState(true);
  const [deleteSessionId, setDeleteSessionId] = useState("");
  const [deleting, setDeleting] = useState(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const followBottomRef = useRef(true);
  const scrollBySessionRef = useRef(new Map<string, number>());

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
  const selectedModel = models.find(item => item.id === model);
  const streamUsage = stream?.usage as Record<string, number> | undefined;
  const contextUsage = contextUsageState(
    streamUsage?.input_tokens
      ?? streamUsage?.inputTokens
      ?? latestReportedInputTokens(visibleRuns),
    selectedModel?.context_window_tokens ?? selectedModel?.contextWindowTokens,
  );
  const slashCommands = visibleComposerCommands(input);

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
  }, [agentId, loadWorkspace]); // 只在切换 Agent 时重置；运行中的刷新由下方轮询负责。

  useEffect(() => {
    setApprovalMode(normalizeApprovalMode(localStorage.getItem(approvalModeStorageKey(agentId))));
    const storedMode = localStorage.getItem(`agentkit:chat:collaboration:${agentId}`);
    setCollaborationMode(storedMode === "plan" ? "plan" : "default");
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
    followBottomRef.current = true;
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function selectSession(sessionId: string) {
    const list = messageListRef.current;
    if (list && currentSessionId) scrollBySessionRef.current.set(currentSessionId, list.scrollTop);
    setCurrentSessionId(sessionId);
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
    const available = Math.max(0, MAX_COMPOSER_ATTACHMENTS - attachments.length);
    if (!available) {
      showToast("附件数量已达上限", `每轮最多 ${MAX_COMPOSER_ATTACHMENTS} 个`, "error");
      return;
    }
    const selected = files.slice(0, available);
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

  async function sendMessage() {
    const submission = parseComposerSubmission(input);
    if (isGenerating) return;
    if (submission.kind === "toggle-plan") {
      togglePlanMode();
      return;
    }
    if (submission.kind === "set-default") {
      changeCollaborationMode("default");
      setInput("");
      showToast("已返回默认模式", "下一轮对话生效", "success");
      return;
    }
    if (submission.kind === "goal" && !submission.objective) {
      showToast("请补充目标", "在 /goal 后输入需要持续完成的目标", "error");
      requestAnimationFrame(() => textareaRef.current?.focus());
      return;
    }
    const goalObjective = submission.kind === "goal" ? submission.objective : "";
    const content = submission.kind === "message"
      ? submission.text || (attachments.length ? "请分析这些附件。" : "")
      : goalObjective;
    if (!content && !attachments.length) return;
    const sessionId = currentSessionId || uniqueId("ses");
    const invocationId = uniqueId("resp");
    const approvalModeForTurn = approvalMode;
    const controller = new AbortController();
    abortRef.current = controller;
    let aggregate: ChatStreamState = {
      ...createChatStreamState(invocationId, sessionId),
      collaborationMode,
      goalObjective,
      startedAt: new Date().toISOString(),
    };
    setCurrentSessionId(sessionId);
    setOptimisticPrompt(content);
    setStream(aggregate);
    setInput("");
    const turnAttachments = attachments;
    setAttachments([]);

    try {
      const response = await apiFetch("/v1/responses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        signal: controller.signal,
        body: JSON.stringify({
          model,
          input: buildResponsesInput(content, turnAttachments),
          stream: true,
          metadata: {
            agent_id: agentId,
            session_id: sessionId,
            invocation_id: invocationId,
            approval_mode: approvalModeForTurn,
            collaboration_mode: collaborationMode,
            goal_objective: goalObjective || undefined,
          },
        }),
      });
      if (!response.ok || !response.body) throw new Error(await responseError(response));

      const decoder = new TextDecoder();
      const parser = createResponseSseParser(event => {
        aggregate = reduceChatStreamEvent(aggregate, event);
        setStream(aggregate);
      });
      const reader = response.body.getReader();
      while (true) {
        const { value, done } = await reader.read();
        if (value) parser.push(decoder.decode(value, { stream: !done }));
        if (done) break;
      }
      parser.finish();
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
        const message = error instanceof Error ? error.message : String(error);
        aggregate = { ...aggregate, status: "failed", error: message };
        setStream(aggregate);
        showToast("运行失败", message, "error");
      }
    } finally {
      abortRef.current = null;
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
      const response = stream?.status === "streaming"
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
      const response = stream?.status === "paused"
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
      const response = stream
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
    name: string,
    data: Record<string, unknown>,
  ) {
    if (!runId) throw new Error("运行尚未创建，请稍后重试");
    const response = await apiFetch(
      `/api/v1/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}:submit`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, data }),
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
    <div className="studio-chat-shell" data-testid="studio-chat-workbench">
      <aside className="chat-session-sidebar" aria-label="会话历史">
        <header className="chat-session-header">
          <div><strong>会话</strong></div>
          <button className="icon-button tertiary" type="button" aria-label="新对话" title="新对话" onClick={startNewSession} disabled={isGenerating}>
            <MessageSquarePlus size={16} />
          </button>
        </header>
        <label className="chat-session-search">
          <span className="sr-only">搜索会话</span>
          <input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索会话" />
        </label>
        <div className="chat-session-list">
          {loading ? (
            <div className="session-empty"><Loader2 size={15} className="animate-spin" /> 加载中</div>
          ) : filteredSessions.length === 0 ? (
            <div className="session-empty">{query ? "没有匹配的会话" : "还没有会话"}</div>
          ) : filteredSessions.map(session => (
            <div key={session.id} className={`chat-session-item${currentSessionId === session.id ? " active" : ""}`}>
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

      <section className="chat-conversation" aria-label={`与 ${agentName} 对话`}>
        <header className="chat-conversation-header">
          <AgentAvatar name={agentName} appearance={agentAppearance} size="sm" />
          <div><strong>{agentName}</strong></div>
          {isGenerating && <span className={`status-badge ${activeStatusLabel}`}>{activeStatusLabel}</span>}
        </header>

        <div
          ref={messageListRef}
          className="chat-message-list"
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
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
              {displayRuns.map(run => <PersistedTurn key={run.id} run={run} agentName={agentName} agentAppearance={agentAppearance} onInteraction={submitInteraction} />)}
              {stream && stream.sessionId === currentSessionId && optimisticPrompt && (
                <StreamingTurn prompt={optimisticPrompt} stream={stream} agentName={agentName} agentAppearance={agentAppearance} onInteraction={submitInteraction} />
              )}
            </>
          )}
        </div>

        <footer className="chat-composer-wrap">
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
          <div className="chat-composer">
            <ComposerCommandMenu
              input={input}
              activeIndex={commandIndex}
              onSelect={selectComposerCommand}
            />
            {attachments.length > 0 && (
              <div className="chat-attachment-list" aria-label="本轮附件">
                {attachments.map(attachment => (
                  <div key={attachment.id} className={`chat-attachment-chip ${attachment.kind}`}>
                    {attachment.kind === "image" && attachment.dataUrl
                      ? <img src={attachment.dataUrl} alt="" />
                      : <span className="chat-attachment-icon"><FileText size={15} /></span>}
                    <span className="chat-attachment-copy">
                      <strong>{attachment.name}</strong>
                      <small>{attachment.kind === "image" ? "图片" : "文本"} · {formatAttachmentSize(attachment.size)}</small>
                    </span>
                    <button
                      type="button"
                      aria-label={`移除附件 ${attachment.name}`}
                      onClick={() => setAttachments(current => current.filter(item => item.id !== attachment.id))}
                    >
                      <X size={13} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              ref={textareaRef}
              rows={1}
              value={input}
              onChange={event => setInput(event.target.value)}
              onKeyDown={event => {
                if (slashCommands.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
                  event.preventDefault();
                  const direction = event.key === "ArrowDown" ? 1 : -1;
                  setCommandIndex(current => (current + direction + slashCommands.length) % slashCommands.length);
                  return;
                }
                if (slashCommands.length && event.key === "Escape") {
                  event.preventDefault();
                  setInput("");
                  return;
                }
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  if (slashCommands.length) {
                    selectComposerCommand(slashCommands[Math.min(commandIndex, slashCommands.length - 1)].id);
                    return;
                  }
                  sendMessage();
                }
              }}
              placeholder={collaborationMode === "plan" ? "描述需要规划的任务…" : "输入消息，或输入 / 使用命令…"}
              aria-label="消息"
              disabled={isGenerating}
            />
            <div className="chat-composer-footer">
              <ComposerActionMenu
                mode={collaborationMode}
                disabled={isGenerating}
                onTogglePlan={togglePlanMode}
                onStartGoal={() => selectComposerCommand("goal")}
                onFiles={addAttachments}
              />
              {collaborationMode === "plan" && (
                <button className="chat-mode-chip" type="button" title="点击返回默认模式" onClick={togglePlanMode}>
                  <ListTodo size={14} />
                  <span>计划</span>
                </button>
              )}
              <ApprovalModeMenu value={approvalMode} onChange={changeApprovalMode} />
              <span className="chat-composer-spacer" />
              <ContextRing {...contextUsage} />
              <ModelMenu
                models={models}
                value={model}
                disabled={models.length <= 1 || isGenerating}
                onChange={setModel}
              />
              {activeStatus === "paused" ? (
                <button className="chat-send-button resume" type="button" aria-label="继续生成" title="继续生成" onClick={resumeResponse}><Play size={15} fill="currentColor" /></button>
              ) : activeStatus === "waiting_input" ? (
                <button className="chat-send-button pause" type="button" aria-label="等待交互输入" title="请先处理上方交互卡片" disabled><Loader2 size={15} className="animate-spin" /></button>
              ) : isGenerating ? (
                <button className="chat-send-button pause" type="button" aria-label="暂停生成" title="暂停生成" onClick={pauseResponse}><Pause size={15} fill="currentColor" /></button>
              ) : (
                <button className="chat-send-button" type="button" aria-label="发送消息" title="发送消息" onClick={sendMessage} disabled={!input.trim() && !attachments.length}><Send size={15} /></button>
              )}
            </div>
          </div>
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
