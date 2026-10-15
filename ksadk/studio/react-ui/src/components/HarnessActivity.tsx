import { useEffect, useState } from "react";
import {
  Bot, Check, ChevronRight, CircleAlert, ListChecks, LoaderCircle,
  ShieldAlert, ShieldCheck, Sparkles,
} from "lucide-react";
import { apiFetch } from "../api";
import { SubagentDetailPanel } from "./SubagentDetail";
import { TaskPlan, ToolExecutionCard, type ToolExecution } from "./ToolExecutionCard";
import "./harnessActivity.css";

export type Activity = {
  id: string; label: string; status: string; kind: string; count?: number;
  durationMs?: number; callId?: string; provider?: string; text?: string;
  durationSource?: "runtime" | "studio_observed";
  category?: "reasoning" | "context" | "recovery" | "tool" | string;
  details: { id: string; text: string; href?: string }[];
  children?: Activity[];
  tools?: ToolExecution[];
};
type Payload = { runId: string; status: string; activities: Activity[];
  approvalAlerts?: { id: string; status: string }[] };
const labels: Record<string, string> = {
  running: "正在运行", completed: "已完成", partial: "部分未完成",
  failed: "执行失败", cancelled: "已取消", unknown: "状态未确认", paused: "等待处理",
};

function safeHref(href?: string) {
  if (!href) return undefined;
  try {
    const url = new URL(href);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

/** 已记录边界的耗时；运行中的开放区间随轮询增长。 */
export function formatActivityDuration(durationMs?: number): string | undefined {
  if (!Number.isFinite(durationMs) || Number(durationMs) < 0) return undefined;
  const seconds = Math.round(Number(durationMs) / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
}

export function ActivityDuration({ activity }: {
  activity: Pick<Activity, "durationMs" | "durationSource">;
}) {
  const duration = formatActivityDuration(activity.durationMs);
  if (!duration) return null;
  const observed = activity.durationSource === "studio_observed";
  return <span className="activity-duration"
    title={observed ? "按记录到的事件时间计算，包含传输与排队时间。" : undefined}>
    {observed ? `观测耗时 ${duration}` : duration}
  </span>;
}

function ActivityIcon({ activity }: { activity: Activity }) {
  if (activity.status === "running") return <LoaderCircle className="activity-spin" size={14} />;
  if (["failed", "partial"].includes(activity.status)) return <CircleAlert size={14} />;
  if (activity.kind === "subagent") return <Bot size={14} />;
  if (activity.kind === "sandbox") return activity.status === "failed" ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />;
  if (activity.status === "completed") return <Check size={14} />;
  return <Sparkles size={14} />;
}

export function ActivityRow({ activity, onOpenDetail, runId }: {
  activity: Activity; runId?: string; onOpenDetail?: (activity: Activity) => void;
}) {
  const status = labels[activity.status] || "状态未确认";
  const sandboxCaps = (activity as Activity & { capabilities?: Record<string, unknown> }).capabilities || {};
  const sandboxCapabilityText = activity.kind === "sandbox"
    ? `文件隔离 ${typeof sandboxCaps.filesystem_isolation === "string" ? sandboxCaps.filesystem_isolation : "未声明"}；网络 ${typeof sandboxCaps.network_control === "string" ? sandboxCaps.network_control : "未声明"}${sandboxCaps.process_boundary === false ? "；无进程边界" : ""}`
    : "";
  const openable = activity.kind === "subagent" && Boolean(activity.callId) && Boolean(onOpenDetail);
  if (openable) {
    return <button type="button" className="harness-subagent-row" data-status={activity.status}
      onClick={() => onOpenDetail?.(activity)}>
      <ActivityIcon activity={activity} />
      <span className="activity-label">{activity.label}</span>
      <ActivityDuration activity={activity} />
      <span className="activity-status">{status}</span>
      <ChevronRight size={13} />
    </button>;
  }
  return <details className="harness-activity-row" data-status={activity.status} data-kind={activity.kind}>
    <summary><ChevronRight className="activity-chevron" size={13} /><ActivityIcon activity={activity} />
      <span className="activity-label">{activity.label}</span>
      {activity.count !== undefined && activity.count > 1 && <span className="activity-status">{activity.count} 次</span>}
      <ActivityDuration activity={activity} />
      <span className="activity-status">{status}</span>
    </summary>
    <div className="harness-activity-detail">
      {activity.tools?.map(tool => <ToolExecutionCard key={tool.id} tool={tool} runId={runId} />)}
      {activity.details.length ? <ul>{activity.details.map(detail => <li key={detail.id}>
        {safeHref(detail.href) ? <a href={safeHref(detail.href)} target="_blank" rel="noopener noreferrer">{detail.text}</a> : detail.text}
      </li>)}</ul> : sandboxCapabilityText ? <p>{sandboxCapabilityText}</p> : !activity.children?.length && !activity.tools?.length ? <p>这段记录没有保存更细的执行步骤。</p> : null}
      {activity.children?.map(child => <ActivityRow key={child.id} activity={child} runId={runId} />)}
    </div>
  </details>;
}

function operationCount(activities: Activity[]): number {
  return activities.reduce((total, activity) => total + Math.max(1, activity.count || 1), 0);
}

function operationSummary(activities: Activity[]): string {
  const names = [...new Set(activities.map(activity => activity.label))];
  return `${names.slice(0, 3).join("、")}${names.length > 3 ? "等" : ""} · ${operationCount(activities)} 项`;
}

/** Routine tool bookkeeping stays available without taking over the chat. */
function ExecutionDetails({ activities, runStatus, runId }: { activities: Activity[]; runStatus?: string; runId?: string }) {
  if (!activities.length) return null;
  const running = activities.some(activity => activity.status === "running");
  // A recovered tool failure is evidence, not the final outcome. Only color the
  // collapsed record as requiring attention when the parent run also ended in
  // a non-success state (or when no authoritative run status is available).
  const hasImperfectStep = activities.some(activity => ["failed", "partial", "unknown"].includes(activity.status));
  const normalizedRunStatus = runStatus?.toLowerCase();
  const attention = hasImperfectStep && (!normalizedRunStatus
    || ["failed", "cancelled", "partial", "unknown"].includes(normalizedRunStatus));
  return <details className="harness-execution-details" data-status={attention ? "attention" : running ? "running" : "completed"}>
    <summary>
      {running ? <LoaderCircle className="activity-spin" size={14} /> : attention ? <CircleAlert size={14} /> : <ListChecks size={14} />}
      <span>执行记录</span>
      <span className="harness-execution-summary">{operationSummary(activities)}</span>
      <ChevronRight className="activity-chevron" size={13} />
    </summary>
    <div className="harness-execution-body">
      {activities.map(activity => <ActivityRow key={activity.id} activity={activity} runId={runId} />)}
    </div>
  </details>;
}

function normalizedProgressText(text: string): string {
  return text.toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, "");
}

function progressSimilarity(left: string, right: string): number {
  const a = normalizedProgressText(left);
  const b = normalizedProgressText(right);
  if (!a || !b) return 0;
  if (a === b || a.includes(b) || b.includes(a)) return 1;
  const grams = (value: string) => new Set(Array.from({ length: Math.max(0, value.length - 1) }, (_, index) => value.slice(index, index + 2)));
  const leftGrams = grams(a);
  const rightGrams = grams(b);
  const shared = [...leftGrams].filter(value => rightGrams.has(value)).length;
  return shared / Math.max(1, Math.min(leftGrams.size, rightGrams.size));
}

/** Keep a readable milestone trail instead of replaying every model update. */
export function compactCommentary(activities: Activity[], limit = 4): Activity[] {
  const deduplicated: Activity[] = [];
  for (const activity of activities) {
    if (activity.kind !== "commentary" || !activity.text) continue;
    const previous = deduplicated.at(-1);
    if (previous?.text && progressSimilarity(previous.text, activity.text) >= 0.62) {
      deduplicated[deduplicated.length - 1] = activity;
    } else {
      deduplicated.push(activity);
    }
  }
  if (deduplicated.length <= limit) return deduplicated;
  return [deduplicated[0], ...deduplicated.slice(-(limit - 1))];
}

/** Fetch only curated execution facts; do not fetch or render private reasoning. */
export function HarnessActivity({ runId, streaming, fallback, onOpenSession }: {
  runId?: string; streaming: boolean; fallback: Activity[];
  onOpenSession?: (sessionId: string) => void;
}) {
  const [payload, setPayload] = useState<Payload | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [detailCallId, setDetailCallId] = useState<string | null>(null);
  useEffect(() => {
    setPayload(null); setUnavailable(false); setDetailCallId(null);
    if (!runId) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let source: EventSource | undefined;
    let streamFailed = false;
    const refresh = async () => {
      let again = streaming;
      try {
        const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/activities`, { signal: abort.signal });
        if (!response.ok) throw new Error("activity unavailable");
        const data: Payload = await response.json();
        if (!Array.isArray(data.activities)) throw new Error("invalid activity response");
        if (!abort.signal.aborted) { setPayload(data); setUnavailable(false); }
        again ||= ["running", "pending", "waiting_input", "paused"].includes(data.status.toLowerCase());
        if (again && !abort.signal.aborted && !streamFailed && typeof EventSource !== "undefined") {
          source = new EventSource(`/api/v1/runs/${encodeURIComponent(runId)}/activities/stream`);
          source.addEventListener("snapshot", event => {
            if (abort.signal.aborted) return;
            try {
              const next: Payload = JSON.parse((event as MessageEvent).data);
              if (!Array.isArray(next.activities)) throw new Error("invalid snapshot");
              setPayload(next); setUnavailable(false);
              if (["completed", "failed", "cancelled", "interrupted", "timed_out"].includes(next.status)) source?.close();
            } catch { source?.close(); streamFailed = true; timer = setTimeout(() => void refresh(), 1000); }
          });
          source.onerror = () => {
            source?.close(); streamFailed = true;
            if (!abort.signal.aborted) timer = setTimeout(() => void refresh(), 1000);
          };
          again = false;
        }
      } catch { if (!abort.signal.aborted) setUnavailable(true); }
      if (again && !abort.signal.aborted) timer = setTimeout(() => void refresh(), 2500);
    };
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); source?.close(); };
  }, [runId, streaming]);

  const activities = payload?.activities.length ? payload.activities : fallback;
  if (!activities.length && !payload?.approvalAlerts?.length) return null;
  const visibleCommentaryIds = new Set(compactCommentary(activities).map(activity => activity.id));
  const plan = activities.flatMap(activity => activity.tools || []).filter(tool =>
    tool.status === "completed" && tool.display.category === "todo" && tool.display.todos,
  ).at(-1)?.display.todos;
  const executionDetails = activities.filter(activity => {
    if (["commentary", "subagent"].includes(activity.kind)) return false;
    if (activity.category === "reasoning" || ["思考", "分析任务", "汇总结果"].includes(activity.label)) return false;
    if (activity.category === "context" || activity.label === "压缩上下文") return false;
    if (activity.kind === "sandbox" && activity.status === "failed") return false;
    return activity.status !== "running";
  });

  return <section className="harness-activity" aria-label="执行过程">
    {payload?.approvalAlerts?.map(alert => <p key={alert.id} role="alert" className="harness-approval-unknown">
      <ShieldAlert size={14} /> 审批结果未确认，当前记录缺少有效决策。请查看审批审计记录。
    </p>)}
    {plan && <TaskPlan items={plan} />}
    {activities.map(activity => {
      if (activity.kind === "commentary" && activity.text) {
        if (!visibleCommentaryIds.has(activity.id)) return null;
        const namedConclusion = activity.label && activity.label !== "公开进展";
        return <div className="harness-activity-commentary" key={activity.id}>
          <Sparkles size={14} aria-hidden="true" />
          <p>{namedConclusion && <strong>{activity.label}：</strong>}{activity.text}</p>
        </div>;
      }
      if (activity.kind === "subagent") return <div className="harness-subagent" key={activity.id}>
        <ActivityRow activity={activity} runId={runId} onOpenDetail={candidate => setDetailCallId(
          detailCallId === candidate.callId ? null : candidate.callId || null,
        )} />
        {runId && detailCallId === activity.callId && <SubagentDetailPanel runId={runId} callId={detailCallId}
          onClose={() => setDetailCallId(null)} onOpenSession={onOpenSession} />}
      </div>;
      if (activity.kind === "sandbox") {
        const caps = (activity as Activity & { capabilities?: Record<string, unknown> }).capabilities || {};
        const rejected = (activity as Activity & { rejected?: boolean }).rejected;
        const fs = typeof caps.filesystem_isolation === "string" ? caps.filesystem_isolation : "";
        const net = typeof caps.network_control === "string" ? caps.network_control : "";
        const pb = caps.process_boundary === false ? "无进程边界" : "";
        if (!rejected && activity.status !== "running") return null;
        return <div className="harness-activity-event harness-activity-sandbox" key={activity.id} data-rejected={rejected ? "true" : "false"}>
          <ActivityRow activity={activity} runId={runId} />
          <p className="harness-activity-sandbox-capabilities">
            {rejected ? "沙箱拒绝执行" : `文件隔离 ${fs || "未声明"}；网络 ${net || "未声明"}${pb ? `；${pb}` : ""}`}
          </p>
        </div>;
      }
      const reasoning = activity.category === "reasoning" || ["思考", "分析任务", "汇总结果"].includes(activity.label);
      const context = activity.category === "context" || activity.label === "压缩上下文";
      const hiddenFinishedReasoning = reasoning && activity.status !== "running";
      const foldedExecution = !reasoning && !context && activity.status !== "running";
      return <div className="harness-activity-event" key={activity.id}>
        {!hiddenFinishedReasoning && !foldedExecution && <ActivityRow activity={activity} runId={runId} />}
      </div>;
    })}
    <ExecutionDetails activities={executionDetails} runStatus={payload?.status} runId={runId} />
    {unavailable && <p className="harness-activity-note">详细步骤暂时无法加载，显示已收到的进度。</p>}
  </section>;
}
