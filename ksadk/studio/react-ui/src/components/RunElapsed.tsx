import { useEffect, useState } from "react";
import { apiFetch } from "../api";

type RunClock = { id: string; status: string; startedAt?: string; completedAt?: string; durationMs?: number | null };
const terminalStates = new Set(["completed", "failed", "cancelled", "interrupted", "timed_out"]);

function durationLabel(milliseconds: number) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

/** ZCode's work header uses the run boundary, never the reasoning component's
 * mount clock. Studio already persists these boundaries on RunRecord. */
export function RunElapsed({ runId, sessionId }: { runId: string; sessionId: string | null }) {
  const [record, setRecord] = useState<RunClock | null>(null);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    setRecord(null);
    if (!sessionId) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      let again = true;
      try {
        const response = await apiFetch(`/api/v1/runs?sessionId=${encodeURIComponent(sessionId)}`, { signal: abort.signal });
        if (!response.ok) throw new Error("run unavailable");
        const data = await response.json();
        const next: RunClock | undefined = Array.isArray(data.items) && data.items.find((item: RunClock) => item.id === runId);
        if (abort.signal.aborted) return;
        if (next) {
          setRecord(next);
          again = !terminalStates.has(next.status.toLowerCase());
        }
      } catch { /* Missing facts never become a made-up timer. */ }
      if (again && !abort.signal.aborted) timer = setTimeout(() => void refresh(), 5000);
    };
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [runId, sessionId]);
  const status = record?.id === runId ? record.status.toLowerCase() : undefined;
  const ended = Boolean(status && terminalStates.has(status));
  useEffect(() => {
    if (!record || ended) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [record, ended]);
  if (!record || record.id !== runId) return null;
  const start = Date.parse(record.startedAt || "");
  const end = Date.parse(record.completedAt || "");
  const duration = ended
    ? Number.isFinite(record.durationMs) && Number(record.durationMs) >= 0 ? Number(record.durationMs)
      : Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : undefined
    : Number.isFinite(start) && now >= start ? now - start : undefined;
  const label = status === "cancelled" ? "已取消" : status === "interrupted" ? "已停止"
    : status === "failed" ? "执行失败" : status === "timed_out" ? "已超时" : ended ? "已工作"
    : ["paused", "waiting_input"].includes(status || "") ? "等待处理" : "工作中";
  return <div className="harness-run-elapsed" data-testid="run-elapsed" data-run-id={runId}
    title="从本轮运行开始计算，包含等待和工具执行时间。">
    {label}{duration !== undefined ? ` ${durationLabel(duration)}` : ""}
  </div>;
}
