import { useEffect, useState } from "react";
import { Bot, Check, ChevronRight, CircleAlert, LoaderCircle, X } from "lucide-react";
import { apiFetch } from "../api";
import { ActivityRow, formatActivityDuration, type Activity } from "./HarnessActivity";

type SubagentDetail = {
  runId: string; callId: string; label: string; status: string;
  durationMs?: number; provider?: string; models?: string[];
  sessionId?: string; parentStatus?: string;
  facts: Activity[];
};
const statusLabels: Record<string, string> = {
  running: "正在运行", completed: "已完成", failed: "执行失败",
  cancelled: "已取消", unknown: "状态未确认",
};

/** 子智能体详情：只展示后端投影过的执行事实，不请求推理或工具原始数据。 */
export function SubagentDetailPanel({ runId, callId, onClose, onOpenSession }: {
  runId: string; callId: string; onClose: () => void;
  onOpenSession?: (sessionId: string) => void;
}) {
  const [detail, setDetail] = useState<SubagentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let again = true;
    const refresh = async () => {
      try {
        const response = await apiFetch(
          `/api/v1/runs/${encodeURIComponent(runId)}/subagents/${encodeURIComponent(callId)}`,
          { signal: abort.signal },
        );
        if (!response.ok) throw new Error("detail unavailable");
        const data: SubagentDetail = await response.json();
        if (!abort.signal.aborted) { setDetail(data); setError(null); }
        again = ["running", "pending", "waiting_input", "paused"].includes(
          String(data.parentStatus || "").toLowerCase(),
        );
      } catch {
        if (!abort.signal.aborted) setError("详情暂时无法加载，请稍后重试。");
        again = false;
      }
      if (again && !abort.signal.aborted) timer = setTimeout(() => void refresh(), 2500);
    };
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [runId, callId]);

  const duration = formatActivityDuration(detail?.durationMs);
  return <div className="subagent-detail-backdrop" onClick={onClose}>
    <aside className="subagent-detail" aria-label="子智能体详情" onClick={event => event.stopPropagation()}>
      <div className="subagent-detail-head">
        <span className="subagent-detail-title"><Bot size={15} /> {detail?.label || "子智能体"}</span>
        <button type="button" className="icon-btn" onClick={onClose} title="关闭"><X size={14} /></button>
      </div>
      {error ? <p className="subagent-detail-note" role="alert">{error}</p>
        : !detail ? <p className="subagent-detail-note">正在读取子智能体执行记录…</p> : (
          <div className="subagent-detail-body">
            <div className="subagent-detail-meta">
              <span className={`subagent-detail-state ${detail.status}`}>
                {detail.status === "running" ? <LoaderCircle size={13} className="activity-spin" />
                  : detail.status === "failed" ? <CircleAlert size={13} /> : <Check size={13} />}
                {statusLabels[detail.status] || "状态未确认"}
              </span>
              {duration && <span>{duration}</span>}
              {detail.provider && <span>Provider：{detail.provider}</span>}
              {detail.models?.length ? <span>模型：{detail.models.join("、")}</span> : null}
            </div>
            <div className="subagent-detail-facts">
              <strong>执行事实</strong>
              {detail.facts.length
                ? detail.facts.map(fact => <ActivityRow key={fact.id} activity={fact} />)
                : <p>暂未记录可展示的操作步骤。</p>}
            </div>
            {detail.sessionId && (
              <button type="button" className="button tertiary small" onClick={() => onOpenSession?.(detail.sessionId!)}>
                <ChevronRight size={13} /> 打开完整子会话
              </button>
            )}
          </div>
        )}
    </aside>
  </div>;
}
