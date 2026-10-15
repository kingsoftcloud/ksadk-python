import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import { ChatMessageList, StatusBanner } from "@kingsoftcloud/ksadk-web/chat/timeline";
import { compactHarnessMessages } from "../compactHarnessMessages";
import { extractLiveThinking, type LiveThinking as LiveThinkingView } from "../liveThinking";
import { HarnessActivity, type Activity } from "./HarnessActivity";
import { LiveThinking } from "./LiveThinking";
import { TimelineTurn } from "./TimelineTurn";

type Props = Omit<ComponentProps<typeof ChatMessageList>, "scrollRef" | "contextIndicator" | "onOpenAttachmentPreview"> & {
  sessionId: string | null;
  /** 当前运行在首条公开消息到达前也已有稳定 run id。 */
  activeRunId?: string;
  /** 本轮思考 token（usage.reasoning_tokens 求和），由宿主在运行结束后结算传入。 */
  reasoningTokens?: number | null;
  hasMoreMessages?: boolean;
  onLoadOlderSessionMessages?: (sessionId: string) => Promise<void>;
  onOpenSession?: (sessionId: string) => void;
};

function activityRunId(message: Props["messages"][number]): string | undefined {
  return [message.invocationId, message.runId, message.id.split(":")[0]].find(id => id && /^run[_-]/.test(id));
}

function fallbackActivity(message: Props["messages"][number]): Activity[] {
  return (message.blocks || []).filter(block => block.type === "thinking").flatMap(block =>
    ("content" in block ? block.content : "").split("\n").filter(Boolean).map((line, index) => {
      const label = line.replace(/^- /, "");
      return { id: `${block.id}:${index}`, label: label.replace(/ · (已完成|正在运行|执行失败|已取消)$/, ""),
        status: /执行失败/.test(label) ? "failed" : /已取消/.test(label) ? "cancelled" : /已完成|^已/.test(label) ? "completed" : /正在/.test(label) ? "running" : "unknown",
        kind: label.includes(" · ") ? "subagent" : "step", details: [] };
    }));
}

function SharedMessage({ message, ...props }: Omit<Props, "messages" | "sessionId" | "onLoadOlderSessionMessages"> & { message: Props["messages"][number] }) {
  const ref = useRef<HTMLDivElement>(null);
  const content = message.role === "model" ? { ...message, reasoning: undefined, blocks: message.blocks?.filter(block => block.type !== "thinking") } : message;
  if (content.role === "model" && !content.blocks?.length && !content.content && !content.attachments?.length
    && !content.a2ui && !content.aguiActivity && !content.aguiActivities?.length && !Object.keys(content.tools || {}).length) return null;
  return <ChatMessageList {...props} messages={[content]} contextIndicator={null} scrollRef={ref} className="harness-shared-message"
    onOpenAttachmentPreview={attachment => {
      const url = new URL(attachment.url, window.location.href);
      if (["http:", "https:", "blob:"].includes(url.protocol)) window.open(url.href, "_blank", "noopener,noreferrer");
    }} />;
}

function hasModelOutput(message: Props["messages"][number]): boolean {
  return message.role === "model" && Boolean(message.content || message.reasoning
    || message.blocks?.some(block => block.type === "text" || block.type === "thinking" ? block.content : true)
    || message.attachments?.length || message.a2ui || message.aguiActivity || message.aguiActivities?.length
    || Object.keys(message.tools || {}).length);
}

function needsPersistentView(message: Props["messages"][number]): boolean {
  // Virtualization must not discard an approval or an interactive form draft.
  return Boolean(message.a2ui && !message.a2ui.ended || message.aguiActivity
    || message.aguiActivities?.length || Object.values(message.tools || {}).some(tool =>
      tool.status === "paused" || tool.approvalStatus === "pending" || tool.status === "running"));
}

/** Shared controller/answer/approval renderer; host-owned expandable activity facts. */
export function CompactHarnessTimeline({ messages, sessionId, activeRunId, reasoningTokens, hasMoreMessages = false, onLoadOlderSessionMessages, onOpenSession, className, revealMessage, ...props }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const heights = useRef(new Map<string, number>());
  const [following, setFollowing] = useState(true);
  const projected = useMemo(() => compactHarnessMessages(messages), [messages]);
  // 实时思考只对直播流生效：历史/压缩后（非流式）仍走摘要口径，detailed
  // reasoning 既不落盘也不在这里回放。运行结束后保留最后一次快照（收起
  // 状态）直到切换会话或下一轮开始，便于展示本轮思考 token。
  const liveThinking = useMemo(
    () => extractLiveThinking(messages, { streaming: Boolean(props.isStreaming) }),
    [messages, props.isStreaming],
  );
  const [pinnedThinking, setPinnedThinking] = useState<{ sessionId: string; view: LiveThinkingView } | null>(null);
  useEffect(() => {
    if (!liveThinking || !sessionId) return;
    setPinnedThinking({ sessionId, view: liveThinking });
  }, [liveThinking, sessionId]);
  const shownThinking: LiveThinkingView | null = props.isStreaming
    ? liveThinking
    : pinnedThinking && pinnedThinking.sessionId === sessionId
      ? { ...pinnedThinking.view, streaming: false }
      : null;

  const displayedRuns = new Set<string>();
  const latestRunId = [...projected].reverse().find(message => message.role === "model");
  // 思考块挂在最后一个 model turn 之前；若压缩结果里没有 model turn（例如
  // 运行以交互卡片收尾），退化为独立的一行，避免思考与 token 消失。
  const liveTurnIndex = projected.reduce((found, message, index) => message.role === "model" ? index : found, -1);
  const activeSession = useRef(sessionId);
  activeSession.current = sessionId;
  const [olderBusy, setOlderBusy] = useState(false);
  const [olderError, setOlderError] = useState(false);
  const olderRequest = useRef<symbol | null>(null);
  const prepend = useRef<{ firstId?: string; height: number; top: number } | null>(null);
  // 流光条件：整个会话还没有任何可见进展（模型输出或 thinking 活动条）。
  // 一旦出现，中间态由 facade 的"正在思考"折叠条接管，避免双指示重复。
  const hasVisibleProgress = messages.some(message => hasModelOutput(message))
    || messages.some(message => (message.blocks || []).some(block => block.type === "thinking"));
  const projectedRunIds = new Set(projected.flatMap(message => {
    const runId = message.role === "model" ? activityRunId(message) : undefined;
    return runId ? [runId] : [];
  }));
  const liveRunWithoutMessage = props.isStreaming && activeRunId && !projectedRunIds.has(activeRunId);
  const waitingForFirstToken = props.isStreaming && !hasVisibleProgress && !activeRunId;
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const onScroll = () => {
      follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100;
      setFollowing(follow.current);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, []);
  useLayoutEffect(() => {
    follow.current = true;
    heights.current.clear();
    setFollowing(true);
    prepend.current = null;
    olderRequest.current = null;
    setOlderBusy(false);
    setOlderError(false);
  }, [sessionId]);
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const anchor = prepend.current;
    if (anchor && anchor.firstId !== projected[0]?.id) {
      element.scrollTop = anchor.top + element.scrollHeight - anchor.height;
      prepend.current = null;
    } else if (follow.current) element.scrollTop = element.scrollHeight;
  }, [projected, sessionId, waitingForFirstToken, hasMoreMessages]);
  const revealedId = projected.find(message => message.id === revealMessage?.id
    || message.sourceMessageIds?.includes(revealMessage?.id || ""))?.id;
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element || !revealMessage || !revealedId) return;
    const row = Array.from(element.querySelectorAll<HTMLElement>('.harness-turn[data-message-id]'))
      .find(node => node.dataset.messageId === revealedId);
    if (!row) return;
    follow.current = false;
    setFollowing(false);
    prepend.current = null;
    element.scrollTop += row.getBoundingClientRect().top - element.getBoundingClientRect().top - 24;
    row.focus({ preventScroll: true });
  }, [revealMessage, revealedId]);
  const loadOlder = async () => {
    if (!sessionId || !hasMoreMessages || !onLoadOlderSessionMessages || olderRequest.current) return;
    const token = Symbol(sessionId);
    olderRequest.current = token;
    const element = scrollRef.current;
    if (element) prepend.current = { firstId: projected[0]?.id, height: element.scrollHeight, top: element.scrollTop };
    follow.current = false;
    setFollowing(false);
    setOlderBusy(true);
    setOlderError(false);
    try {
      await onLoadOlderSessionMessages(sessionId);
    } catch {
      if (activeSession.current === sessionId && olderRequest.current === token) setOlderError(true);
    } finally {
      if (activeSession.current === sessionId && olderRequest.current === token) {
        olderRequest.current = null;
        setOlderBusy(false);
      }
    }
  };
  return <div ref={scrollRef} className={`harness-timeline${className ? ` ${className}` : ""}`}>
    {sessionId && onLoadOlderSessionMessages && hasMoreMessages && <div className="harness-history-loader">
      <button className="button tertiary small" type="button" disabled={olderBusy} aria-busy={olderBusy}
        onClick={() => void loadOlder()}>{olderBusy ? "正在加载更早消息…" : "加载更早消息"}</button>
      {olderError && <span role="alert">加载失败，请重试</span>}
    </div>}
    {!projected.length && !props.isStreaming ? props.emptyState : projected.map((message, index) => {
      const runId = activityRunId(message);
      const fallback = fallbackActivity(message);
      const showActivity = message.role === "model"
        && (runId ? !displayedRuns.has(runId) : fallback.length > 0);
      if (showActivity && runId) displayedRuns.add(runId);
      const liveTurn = Boolean(shownThinking) && index === liveTurnIndex;
      return <TimelineTurn key={`${sessionId}:${message.id}`} id={message.id} root={scrollRef} heights={heights.current}
        live={index >= projected.length - 4} pinned={needsPersistentView(message)
          || revealedId === message.id || index === liveTurnIndex && Boolean(shownThinking)}
        follow={follow} revealed={revealedId === message.id}>
      {showActivity && <HarnessActivity runId={runId}
        streaming={props.isStreaming && Boolean(latestRunId && activityRunId(latestRunId) === runId)} fallback={fallback} onOpenSession={onOpenSession} />}
      {liveTurn && shownThinking && <LiveThinking key={shownThinking.runId} text={shownThinking.text} streaming={shownThinking.streaming}
        reasoningTokens={reasoningTokens} />}
      <SharedMessage {...props} message={message} isStreaming={props.isStreaming && index === projected.length - 1} />
    </TimelineTurn>; })}
    {liveRunWithoutMessage && <div className="harness-turn harness-live-run" data-run-id={activeRunId}>
      <HarnessActivity
        runId={activeRunId}
        streaming
        fallback={[{
          id: `${activeRunId}:starting`,
          kind: "step",
          label: "分析任务",
          status: "running",
          details: [{ id: `${activeRunId}:starting:detail`, text: "正在理解任务并准备执行" }],
        }]}
        onOpenSession={onOpenSession}
      />
    </div>}
    {shownThinking && liveTurnIndex < 0 && <div className="harness-turn">
      <LiveThinking key={shownThinking.runId} text={shownThinking.text} streaming={shownThinking.streaming} reasoningTokens={reasoningTokens} />
    </div>}
    {waitingForFirstToken && <div className="harness-thinking" role="status">
      <span className="text-shimmer">正在思考…</span>
    </div>}
    <StatusBanner />
  {!following && <button className="harness-return-bottom button secondary small" type="button" onClick={() => {
    follow.current = true; setFollowing(true);
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }}>回到最新消息</button>}</div>;
}
