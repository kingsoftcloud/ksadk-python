import { useEffect, useRef, useState } from "react";
import { ChevronRight, Search, Terminal } from "lucide-react";
import { ToolExecutionCard } from "./ToolExecutionCard";
import { LatestSummaryLine } from "./LatestSummaryLine";
import { exploreCounts, type ToolWorkItem } from "./toolWorkItems";

export function ToolExecutionGroup({ item, runId }: { item: ToolWorkItem; runId?: string }) {
  const [open, setOpen] = useState(false);
  const issues = item.tools.filter(tool => ["failed", "unknown", "paused"].includes(tool.status));
  const failed = issues.filter(tool => tool.status === "failed").length;
  const unknown = issues.filter(tool => tool.status === "unknown").length;
  // Failures remain immediately actionable even in a long collapsed group.
  const hadIssues = useRef(false);
  useEffect(() => {
    if (issues.length && !hadIssues.current) setOpen(true);
    hadIssues.current = issues.length > 0;
  }, [issues.length]);
  const latest = [...item.tools].reverse().find(tool => tool.status === "running") || item.tools.at(-1)!;
  const label = item.family === "explore" ? "查阅" : "终端";
  const counts = item.family === "explore" ? exploreCounts(item.tools) : `${item.tools.length} 命令`;
  const Icon = item.family === "explore" ? Search : Terminal;
  return <details className="harness-tool-group" open={open}
    data-group-id={`${item.family}:${item.tools[0].callId}`} data-running={item.running}
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary aria-label={`${label} · ${counts}${item.running ? " · 工作中" : ""}`}>
      <Icon size={16} aria-hidden="true" /><span className="harness-tool-action">{label}</span><span aria-hidden="true">·</span>
      {item.running && !open
        ? <><span className="harness-stage-running">正在执行</span><LatestSummaryLine text={latest.display.summary || counts} streaming /></>
        : <span className="harness-tool-group-counts">{counts}</span>}
      {failed > 0 && <span className="harness-group-issue">{failed} 项失败</span>}
      {unknown > 0 && <span className="harness-group-issue">{unknown} 项结果未确认</span>}
      {issues.some(tool => tool.status === "paused") && <span className="harness-group-issue">等待处理</span>}
      <ChevronRight size={16} className="activity-chevron harness-tool-disclosure" aria-hidden="true" />
    </summary>
    {open && <div className="harness-tool-group-children">{item.tools.map(tool =>
      <ToolExecutionCard key={tool.callId} tool={tool} runId={runId} />
    )}</div>}
  </details>;
}
