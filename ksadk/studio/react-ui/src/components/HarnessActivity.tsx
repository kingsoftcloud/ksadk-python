import { useEffect, useState } from "react";
import { Bot, Check, ChevronRight, CircleAlert, LoaderCircle, Sparkles } from "lucide-react";
import { apiFetch } from "../api";
import "./harnessActivity.css";

export type Activity = {
  id: string; label: string; status: string; kind: string; count?: number;
  details: { id: string; text: string; href?: string }[];
  children?: Activity[];
};
type Payload = { runId: string; status: string; activities: Activity[] };
const labels: Record<string, string> = { running: "正在运行", completed: "已完成", failed: "执行失败", cancelled: "已取消", unknown: "状态未确认", paused: "等待处理" };

function safeHref(href?: string) {
  if (!href) return undefined;
  try { const url = new URL(href); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}

export function ActivityRow({ activity }: { activity: Activity }) {
  const status = labels[activity.status] || "状态未确认";
  const Icon = activity.status === "running" ? LoaderCircle : activity.status === "failed" ? CircleAlert : activity.kind === "subagent" ? Bot : activity.status === "completed" ? Check : Sparkles;
  return <details className="harness-activity-row" data-status={activity.status} data-kind={activity.kind}>
    <summary><ChevronRight className="activity-chevron" size={13} /><Icon className={activity.status === "running" ? "activity-spin" : ""} size={14} />
      <span className="activity-label">{activity.label}</span>
      {activity.count !== undefined && activity.count > 1 && <span className="activity-status">{activity.count} 次</span>}
      <span className="activity-status">{status}</span>
    </summary>
    <div className="harness-activity-detail">
      {activity.details.length ? <ul>{activity.details.map(detail => <li key={detail.id}>
        {safeHref(detail.href) ? <a href={safeHref(detail.href)} target="_blank" rel="noopener noreferrer">{detail.text}</a> : detail.text}
      </li>)}</ul> : !activity.children?.length ? <p>这段记录没有保存更细的执行步骤。</p> : null}
      {activity.children?.map(child => <ActivityRow key={child.id} activity={child} />)}
    </div>
  </details>;
}

/** Fetch only curated execution facts; do not fetch or render private reasoning. */
export function HarnessActivity({ runId, streaming, fallback }: { runId?: string; streaming: boolean; fallback: Activity[] }) {
  const [payload, setPayload] = useState<Payload | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    setPayload(null); setUnavailable(false);
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
    {activities.map(activity => <ActivityRow key={activity.id} activity={activity} />)}
    {unavailable && <p className="harness-activity-note">详细步骤暂时无法加载，显示已收到的进度。</p>}
  </section>;
}
