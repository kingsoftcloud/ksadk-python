import { useEffect, useState } from "react";
import { Bot, Check, ChevronRight, CircleAlert, LoaderCircle, Sparkles } from "lucide-react";
import { apiFetch } from "../api";
import { SubagentDetailPanel } from "./SubagentDetail";
import "./harnessActivity.css";

export type Activity = {
  id: string; label: string; status: string; kind: string; count?: number;
  durationMs?: number; callId?: string; provider?: string; text?: string;
  details: { id: string; text: string; href?: string }[];
  children?: Activity[];
};
type Payload = { runId: string; status: string; activities: Activity[] };
const labels: Record<string, string> = { running: "正在运行", completed: "已完成", partial: "部分未完成", failed: "执行失败", cancelled: "已取消", unknown: "状态未确认", paused: "等待处理" };

function safeHref(href?: string) {
  if (!href) return undefined;
  try { const url = new URL(href); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}

/** 后端给的 durationMs 是执行证据推导值；运行中会随轮询增长。 */
export function formatActivityDuration(durationMs?: number): string | undefined {
  if (!Number.isFinite(durationMs) || Number(durationMs) < 0) return undefined;
  const seconds = Math.round(Number(durationMs) / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
}

export function ActivityRow({ activity, onOpenDetail }: { activity: Activity; onOpenDetail?: (activity: Activity) => void }) {
  const status = labels[activity.status] || "状态未确认";
  const duration = formatActivityDuration(activity.durationMs);
  const Icon = activity.status === "running" ? LoaderCircle : ["failed", "partial"].includes(activity.status) ? CircleAlert : activity.kind === "subagent" ? Bot : activity.status === "completed" ? Check : Sparkles;
  const openable = activity.kind === "subagent" && Boolean(activity.callId) && Boolean(onOpenDetail);
  return <details className="harness-activity-row" data-status={activity.status} data-kind={activity.kind}>
    <summary><ChevronRight className="activity-chevron" size={13} /><Icon className={activity.status === "running" ? "activity-spin" : ""} size={14} />
      <span className="activity-label">{activity.label}</span>
      {activity.count !== undefined && activity.count > 1 && <span className="activity-status">{activity.count} 次</span>}
      {duration && <span className="activity-duration">{duration}</span>}
      <span className="activity-status">{status}</span>
      {openable && <button type="button" className="activity-detail-entry"
        onClick={event => { event.preventDefault(); event.stopPropagation(); onOpenDetail?.(activity); }}>详情</button>}
    </summary>
    <div className="harness-activity-detail">
      {activity.details.length ? <ul>{activity.details.map(detail => <li key={detail.id}>
        {safeHref(detail.href) ? <a href={safeHref(detail.href)} target="_blank" rel="noopener noreferrer">{detail.text}</a> : detail.text}
      </li>)}</ul> : !activity.children?.length ? <p>这段记录没有保存更细的执行步骤。</p> : null}
      {activity.children?.map(child => <ActivityRow key={child.id} activity={child} onOpenDetail={onOpenDetail} />)}
    </div>
  </details>;
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
    const refresh = async () => {
      let again = streaming;
      try {
        const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/activities`, { signal: abort.signal });
        if (!response.ok) throw new Error("activity unavailable");
        const data: Payload = await response.json();
        if (!Array.isArray(data.activities)) throw new Error("invalid activity response");
        if (!abort.signal.aborted) { setPayload(data); setUnavailable(false); }
        again ||= ["running", "pending", "waiting_input", "paused"].includes(data.status.toLowerCase());
      } catch { if (!abort.signal.aborted) setUnavailable(true); }
      if (again && !abort.signal.aborted) timer = setTimeout(() => void refresh(), 2500);
    };
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [runId, streaming]);
  const activities = payload?.activities.length ? payload.activities : fallback;
  if (!activities.length) return null;
  return <section className="harness-activity" aria-label="执行过程">
    {activities.map(activity => {
      if (activity.kind === "commentary" && activity.text) {
        const namedConclusion = activity.label && activity.label !== "公开进展";
        return <p className="harness-activity-commentary" key={activity.id}>
          {namedConclusion && <strong>{activity.label}：</strong>}{activity.text}
        </p>;
      }
      // Finished model-call phases are implementation scaffolding. Real
      // public progress is rendered above from commentary events; keep only a
      // currently running phase so a fresh run never looks idle.
      const hiddenFinishedPhase = activity.status === "completed"
        && ["分析任务", "汇总结果"].includes(activity.label);
      return <div className="harness-activity-event" key={activity.id}>
        {!hiddenFinishedPhase && <ActivityRow activity={activity} onOpenDetail={candidate => setDetailCallId(candidate.callId || null)} />}
      </div>;
    })}
    {unavailable && <p className="harness-activity-note">详细步骤暂时无法加载，显示已收到的进度。</p>}
    {runId && detailCallId && <SubagentDetailPanel runId={runId} callId={detailCallId}
      onClose={() => setDetailCallId(null)} onOpenSession={onOpenSession} />}
  </section>;
}
