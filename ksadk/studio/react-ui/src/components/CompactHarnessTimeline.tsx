import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import { ChatMessageList, StatusBanner } from "@kingsoftcloud/ksadk-web/chat/timeline";
import { compactHarnessMessages } from "../compactHarnessMessages";
import { ContextActivity, HarnessActivity, type Activity } from "./HarnessActivity";
import { LiveThinking } from "./LiveThinking";
import { RunElapsed } from "./RunElapsed";
import { TimelineTurn } from "./TimelineTurn";
import { TaskPlan, ToolExecutionCard, type ToolDisplay, type PlanRunState } from "./ToolExecutionCard";
import { ToolExecutionGroup } from "./ToolExecutionGroup";
import { buildToolWorkItems, executionFromBlock } from "./toolWorkItems";

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
  // The shared live controller can retain the canonical message id while
  // dropping its run metadata. This is the projector's exact id envelope,
  // not a run id guessed from message content or the currently active run.
  const canonicalRun = /^conversation:(run[_-][^:]+):[^:]+$/.exec(message.id)?.[1];
  return [message.runId, message.invocationId, canonicalRun, message.id.split(":")[0]]
    .find(id => id && /^run[_-]/.test(id));
}

function fallbackActivity(message: Props["messages"][number]): Activity[] {
  return (message.blocks || []).filter(block => block.type === "thinking"
    && block.source !== "public_summary" && block.source !== "provider_reasoning").flatMap(block =>
    ("content" in block ? block.content : "").split("\n").filter(Boolean).map((line, index) => {
      const label = line.replace(/^- /, "");
      return { id: `${block.id}:${index}`, label: label.replace(/ · (已完成|正在运行|执行失败|已取消)$/, ""),
        status: /执行失败/.test(label) ? "failed" : /已取消/.test(label) ? "cancelled" : /已完成|^已/.test(label) ? "completed" : /正在/.test(label) ? "running" : "unknown",
        kind: label.includes(" · ") ? "subagent" : "step", details: [] };
    }));
}

function SharedMessage({ message, ...props }: Omit<Props, "messages" | "sessionId" | "onLoadOlderSessionMessages"> & { message: Props["messages"][number] }) {
  const ref = useRef<HTMLDivElement>(null);
  const content = message.role === "model" ? { ...message, reasoning: undefined,
    blocks: message.blocks?.filter(block => block.type !== "thinking" || block.source === "public_summary" || block.source === "provider_reasoning") } : message;
  const work = buildToolWorkItems(content.blocks || [], props.isStreaming);
  if (message.contextActivity) return <ContextActivity activity={{ id: message.id, kind: "step", category: "context",
    label: message.content, details: [], ...message.contextActivity }} />;
  if (content.role === "model" && !content.blocks?.length && !content.content && !content.attachments?.length
    && !content.a2ui && !content.aguiActivity && !content.aguiActivities?.length && !Object.keys(content.tools || {}).length) return null;
  return <ChatMessageList {...props} showAgentHeader={false} messages={[content]} contextIndicator={null} scrollRef={ref} className="harness-shared-message"
    renderProcessingBlock={block => {
      if (block.type === "thinking" && (block.source === "public_summary" || block.source === "provider_reasoning")) {
        return <LiveThinking key={block.id} text={block.content}
          streaming={Boolean(props.isStreaming && block.status === "streaming")} />;
      }
      if (work.groupedChildren.has(block.id)) return null;
      const item = work.first.get(block.id);
      if (!item) return undefined;
      if (item.tools[0].display.category === "todo" && item.tools.every(tool => tool.status === "completed")) return null;
      return item.family
        ? <ToolExecutionGroup key={item.tools[0].callId} item={item} runId={activityRunId(message)} />
        : <ToolExecutionCard key={item.tools[0].callId} runId={activityRunId(message)} tool={item.tools[0]} />;
    }}
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
  const activeSession = useRef(sessionId);
  activeSession.current = sessionId;
  const [runRefs, setRunRefs] = useState<{ sessionId: string | null; byRef: Record<string, string> }>({ sessionId, byRef: {} });
  const receiveRun = useCallback((reference: string, runId: string) => {
    if (activeSession.current !== sessionId || reference === runId) return;
    setRunRefs(previous => previous.sessionId === sessionId && previous.byRef[reference] === runId ? previous
      : { sessionId, byRef: { ...(previous.sessionId === sessionId ? previous.byRef : {}), [reference]: runId } });
  }, [sessionId]);
  const projected = useMemo(() => compactHarnessMessages(messages.map(message => {
    const reference = activityRunId(message);
    const runId = runRefs.sessionId === sessionId && reference ? runRefs.byRef[reference] : undefined;
    // Normalize before compaction so adjacent canonical items from separate
    // runs cannot be merged and inherit one another's clock or activity.
    return runId || reference ? { ...message, runId: runId || reference } : message;
  })), [messages, runRefs, sessionId]);
  const thinkingRuns = new Set(projected.flatMap(message => message.role === "model"
    && props.isStreaming && message.blocks?.some(block => block.type === "thinking"
      && block.status === "streaming" && (block.source === "public_summary" || block.source === "provider_reasoning"))
    ? [activityRunId(message)] : []));
  const liveRunId = activeRunId && runRefs.sessionId === sessionId ? runRefs.byRef[activeRunId] || activeRunId : activeRunId;
  const [planFacts, setPlanFacts] = useState<{ sessionId: string | null; byRun: Record<string, NonNullable<ToolDisplay["todos"]>> }>({ sessionId, byRun: {} });
  const [runStates, setRunStates] = useState<{ sessionId: string | null; byRun: Record<string, PlanRunState> }>({ sessionId, byRun: {} });
  const receiveRunState = useCallback((runId: string, state: PlanRunState) => {
    if (activeSession.current !== sessionId) return;
    setRunStates(previous => {
      const prior = previous.sessionId === sessionId ? previous.byRun[runId] : undefined;
      if (prior?.status === state.status && prior?.outcome === state.outcome) return previous;
      return { sessionId, byRun: { ...(previous.sessionId === sessionId ? previous.byRun : {}), [runId]: state } };
    });
  }, [sessionId]);
  const receivePlan = useCallback((runId: string, items: NonNullable<ToolDisplay["todos"]>) => {
    if (activeSession.current !== sessionId) return;
    setPlanFacts(previous => ({ sessionId,
      byRun: { ...(previous.sessionId === sessionId ? previous.byRun : {}), [runId]: items },
    }));
  }, [sessionId]);
  // A follow-up answer without TodoWrite must not erase the latest session
  // plan. Order by transcript, not by the arrival order of run fetches. Older
  // sessions can carry the plan only in curated activity receipts.
  const latestPlan = projected.flatMap(message => message.role === "model"
    ? [planFacts.sessionId === sessionId ? planFacts.byRun[activityRunId(message) || ""] : undefined,
      ...(message.blocks || []).flatMap(block => {
        const tool = executionFromBlock(block);
        return tool?.status === "completed" && tool.display.category === "todo" && tool.display.todos?.length
          ? [tool.display.todos] : [];
      })].filter((plan): plan is NonNullable<ToolDisplay["todos"]> => Boolean(plan))
        .map(items => ({ runId: activityRunId(message), items })) : []).at(-1)
    || (planFacts.sessionId === sessionId && liveRunId && planFacts.byRun[liveRunId]
      ? { runId: liveRunId, items: planFacts.byRun[liveRunId] } : undefined);
  // Public segments stay at their canonical position and retain their item id.
  // Never turn unlabelled reasoning or activity text into an authorized summary.
  void reasoningTokens;

  const displayedRuns = new Set<string>();
  const latestRunId = [...projected].reverse().find(message => message.role === "model");
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
  const liveRunWithoutMessage = props.isStreaming && liveRunId && !projectedRunIds.has(liveRunId);
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
  return <div className="harness-timeline-shell"><div className="harness-plan-dock" aria-label="任务进度窗口">
    {latestPlan && <TaskPlan key={`${sessionId}:${latestPlan.runId}`} items={latestPlan.items}
      runState={runStates.sessionId === sessionId ? runStates.byRun[latestPlan.runId || ""] : undefined}
      previousRun={Boolean(latestPlan.runId && (props.isStreaming && liveRunId || latestRunId && activityRunId(latestRunId)) !== latestPlan.runId)} />}</div>
    <div ref={scrollRef} className={`harness-timeline${className ? ` ${className}` : ""}`}><div className="harness-timeline-content">
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
      return <TimelineTurn key={`${sessionId}:${message.id}`} id={message.id} root={scrollRef} heights={heights.current}
        live={index >= projected.length - 4} pinned={needsPersistentView(message)
          || revealedId === message.id}
        follow={follow} revealed={revealedId === message.id}>
      {showActivity && runId && <RunElapsed runId={runId} sessionId={sessionId} />}
      <SharedMessage {...props} message={message} isStreaming={props.isStreaming && index === projected.length - 1} />
      {showActivity && <HarnessActivity runId={runId}
        showExecutionDetails={false} showPlan={false}
        onPlan={receivePlan}
        onRunState={receiveRunState}
        onResolvedRun={receiveRun}
        canonicalTimeline
        visibleThinking={thinkingRuns.has(runId)}
        streaming={props.isStreaming && Boolean(latestRunId && activityRunId(latestRunId) === runId)} fallback={fallback} onOpenSession={onOpenSession} />}
    </TimelineTurn>; })}
    {liveRunWithoutMessage && <div className="harness-turn harness-live-run" data-run-id={liveRunId}>
      <RunElapsed runId={liveRunId} sessionId={sessionId} />
      <HarnessActivity
        runId={liveRunId}
        onResolvedRun={receiveRun}
        canonicalTimeline
        showExecutionDetails={false} showPlan={false}
        onPlan={receivePlan}
        onRunState={receiveRunState}
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
    {waitingForFirstToken && <div className="harness-thinking" role="status">
      <span className="text-shimmer">正在思考…</span>
    </div>}
    <StatusBanner />
    </div>
  {!following && <button className="harness-return-bottom button secondary small" type="button" onClick={() => {
    follow.current = true; setFollowing(true);
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }}>回到最新消息</button>}</div></div>;
}
