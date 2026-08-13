import { useEffect, useMemo, useState } from "react";
import { Clock3, ListTodo, Pause, Play, Square, Target } from "lucide-react";

export type RuntimeMode = "plan" | "goal";
export type RuntimeModeStatus = "running" | "paused" | "waiting";

interface RuntimeModeBarProps {
  mode: RuntimeMode;
  status: RuntimeModeStatus;
  objective: string;
  startedAt?: string;
  elapsedMs?: number | null;
  now?: number;
  onPause?: () => void;
  onResume?: () => void;
  onStop: () => void;
}

function elapsedLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${remainder}秒`;
  return `${remainder}秒`;
}

function startLabel(value?: string): string {
  if (!value) return "刚刚启动";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "刚刚启动";
  return `${new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date)} 启动`;
}

export function RuntimeModeBar({
  mode,
  status,
  objective,
  startedAt,
  elapsedMs,
  now,
  onPause,
  onResume,
  onStop,
}: RuntimeModeBarProps) {
  const [clock, setClock] = useState(now ?? Date.now());
  const startedAtMs = useMemo(() => startedAt ? Date.parse(startedAt) : Number.NaN, [startedAt]);
  const running = status === "running";
  const waiting = status === "waiting";
  const resumable = status === "paused";
  const ModeIcon = mode === "goal" ? Target : ListTodo;
  const title = waiting
    ? mode === "goal" ? "目标等待输入" : "计划等待输入"
    : resumable
      ? mode === "goal" ? "目标已暂停" : "计划已暂停"
      : mode === "goal" ? "目标执行中" : "正在规划";
  const elapsed = elapsedMs != null
    ? elapsedMs
    : Number.isFinite(startedAtMs) ? Math.max(0, clock - startedAtMs) : 0;

  useEffect(() => {
    if (!running || now != null) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [now, running]);

  return (
    <div className={`runtime-mode-bar ${mode} ${status}`} data-testid="runtime-mode-bar">
      <span className="runtime-mode-icon"><ModeIcon size={15} /></span>
      <div className="runtime-mode-copy">
        <span><strong>{title}</strong><span>{objective}</span></span>
        <small><Clock3 size={12} /> {startLabel(startedAt)} · {elapsedLabel(elapsed)}</small>
      </div>
      <div className="runtime-mode-actions">
        {running && onPause && (
          <button type="button" onClick={onPause} aria-label={`暂停${mode === "goal" ? "目标" : "计划"}`} title="暂停">
            <Pause size={14} fill="currentColor" />
          </button>
        )}
        {resumable && onResume && (
          <button type="button" onClick={onResume} aria-label={`继续${mode === "goal" ? "目标" : "计划"}`} title="继续">
            <Play size={14} fill="currentColor" />
          </button>
        )}
        <button type="button" onClick={onStop} aria-label={`结束${mode === "goal" ? "目标" : "计划"}`} title="结束">
          <Square size={13} fill="currentColor" />
        </button>
      </div>
    </div>
  );
}
