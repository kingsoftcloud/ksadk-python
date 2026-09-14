import { useEffect, useMemo, useRef, type ComponentProps } from "react";
import { ChatMessageList, StatusBanner } from "@kingsoftcloud/ksadk-web/chat/timeline";
import { compactHarnessMessages } from "../compactHarnessMessages";
import { HarnessActivity, type Activity } from "./HarnessActivity";

type Props = Omit<ComponentProps<typeof ChatMessageList>, "scrollRef" | "contextIndicator" | "onOpenAttachmentPreview"> & {
  sessionId: string | null;
  onLoadOlderSessionMessages?: (sessionId: string) => Promise<void>;
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

/** Shared controller/answer/approval renderer; host-owned expandable activity facts. */
export function CompactHarnessTimeline({ messages, sessionId, onLoadOlderSessionMessages, className, ...props }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const projected = useMemo(() => compactHarnessMessages(messages), [messages]);
  const displayedRuns = new Set<string>();
  const latestRunId = [...projected].reverse().find(message => message.role === "model");
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const onScroll = () => { follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100; };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => { follow.current = true; }, [sessionId]);
  useEffect(() => {
    const element = scrollRef.current;
    if (follow.current && element) element.scrollTop = element.scrollHeight;
  }, [projected]);
  return <div ref={scrollRef} className={`harness-timeline${className ? ` ${className}` : ""}`}>
    {sessionId && onLoadOlderSessionMessages && <button className="tertiary" type="button"
      onClick={() => void onLoadOlderSessionMessages(sessionId)}>加载更早消息</button>}
    {!projected.length ? props.emptyState : projected.map((message, index) => {
      const runId = activityRunId(message);
      const showActivity = message.role === "model" && (!runId || !displayedRuns.has(runId));
      if (showActivity && runId) displayedRuns.add(runId);
      return <div className="harness-turn" key={message.id}>
      {showActivity && <HarnessActivity runId={runId}
        streaming={props.isStreaming && Boolean(latestRunId && activityRunId(latestRunId) === runId)} fallback={fallbackActivity(message)} />}
      <SharedMessage {...props} message={message} isStreaming={props.isStreaming && index === projected.length - 1} />
    </div>; })}
    <StatusBanner />
  </div>;
}
