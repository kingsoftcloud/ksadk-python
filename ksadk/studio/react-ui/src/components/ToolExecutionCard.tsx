import { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, CircleAlert, FileText, Search, Terminal } from "lucide-react";

export type ToolDisplay = {
  version: "tool-display/v1";
  category: "terminal" | "read" | "search" | "edit" | "todo" | "tool";
  summary: string; path?: string; output?: string; diff?: string;
  truncated?: boolean; total_chars?: number; exit_code?: number; artifact_id?: string;
  todos?: { id: string; content: string; status: "pending" | "in_progress" | "completed"; priority: string }[];
};
export type ToolExecution = { id: string; callId: string; status: string; display: ToolDisplay };
const names = { terminal: "终端", read: "读取文件", search: "搜索", edit: "文件修改", todo: "更新计划", tool: "工具调用" };
const statuses: Record<string, string> = { running: "执行中", completed: "已完成", failed: "失败", unknown: "结果未确认", cancelled: "已取消" };

export function diffLines(text: string) {
  let oldLine: number | undefined, newLine: number | undefined;
  return text.split("\n").map(line => {
    const header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (header) { oldLine = Number(header[1]); newLine = Number(header[2]); }
    const change = line.startsWith("+") && !line.startsWith("+++") ? "add"
      : line.startsWith("-") && !line.startsWith("---") ? "remove" : "context";
    const numbered = !header && oldLine !== undefined && newLine !== undefined
      && !line.startsWith("\\") && /^[ +\-]/.test(line)
      && !line.startsWith("+++") && !line.startsWith("---");
    const before = numbered && change !== "add" ? oldLine : undefined;
    const after = numbered && change !== "remove" ? newLine : undefined;
    if (numbered) {
      if (change !== "add") oldLine!++;
      if (change !== "remove") newLine!++;
    }
    if (line.startsWith("diff ") || line.startsWith("---") || line.startsWith("+++")) {
      oldLine = undefined; newLine = undefined;
    }
    return { line, change, before, after };
  });
}

/** Text-only public output: never HTML/ANSI, no raw args or receipts. */
export function ToolExecutionCard({ tool, runId }: { tool: ToolExecution; runId?: string }) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [following, setFollowing] = useState(true);
  const [copyState, setCopyState] = useState("");
  const outputRef = useRef<HTMLPreElement>(null);
  const display = tool.display;
  const text = display.diff || display.output || "";
  useEffect(() => {
    if (following && display.category === "terminal" && outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight;
  }, [text, open, following, display.category]);
  if (display.version !== "tool-display/v1") return null;
  const Icon = display.category === "terminal" ? Terminal : display.category === "search" ? Search : FileText;
  return <details className="harness-tool-card" open={open} data-status={tool.status} data-category={display.category}
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><ChevronRight size={13} className="activity-chevron" /><Icon size={14} />
      <span>{display.summary || display.path || names[display.category]}</span>
      <span className="activity-status">{statuses[tool.status] || "状态未确认"}</span>
      {display.exit_code !== undefined && <span className="activity-status">退出码 {display.exit_code}</span>}
    </summary>
    {open && <div className="harness-tool-body">
      {display.path && <p className="harness-tool-path">{display.path}</p>}
      {text ? <>
        <div className="harness-tool-actions">
          <button type="button" onClick={() => setExpanded(value => !value)}>{expanded ? "收起高度" : "展开高度"}</button>
          <button type="button" onClick={() => void navigator.clipboard.writeText(text).then(
            () => setCopyState("已复制"), () => setCopyState("复制失败"),
          )}>复制{display.diff ? " diff" : "输出"}</button>
          <span role="status">{copyState}</span>
          {!following && display.category === "terminal" && tool.status === "running" && <button type="button" onClick={() => setFollowing(true)}>跟随最新输出</button>}
        </div>
        <pre ref={outputRef} className="harness-tool-output" data-expanded={expanded} tabIndex={0}
          aria-label={display.diff ? "编辑 diff" : "工具输出"}
          onScroll={event => { const node = event.currentTarget;
            setFollowing(node.scrollHeight - node.clientHeight - node.scrollTop < 24);
          }}>
          {display.diff ? diffLines(text).map(({ line, change, before, after }, index) => <span className="harness-diff-line" key={index}
            data-change={change}>
            <span className="harness-diff-number" aria-hidden="true">{before ?? ""}</span>
            <span className="harness-diff-number" aria-hidden="true">{after ?? ""}</span>{line}{"\n"}
          </span>) : text}
        </pre>
      </> : <p>未记录可公开展示的输出。完整审计记录可在检查器中查看。</p>}
      {display.truncated && <p className="harness-tool-note">输出已截断{display.total_chars ? `（原始 ${display.total_chars.toLocaleString()} 字符）` : ""}，展开高度仅显示已收到的部分。</p>}
      {runId && /^[a-f0-9]{64}$/.test(display.artifact_id || "") && <p className="harness-tool-note">
        <a href={`/api/v1/runs/${encodeURIComponent(runId)}/public-output/${display.artifact_id}`} download>下载已保留输出</a>
        （已脱敏；宿主输出上限之外的内容无法恢复）
      </p>}
    </div>}
  </details>;
}

export function TaskPlan({ items }: { items: NonNullable<ToolDisplay["todos"]> }) {
  const completed = items.filter(item => item.status === "completed").length;
  const current = items.find(item => item.status === "in_progress");
  const [open, setOpen] = useState(true);
  return <details className="harness-task-plan" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><ChevronRight size={13} className="activity-chevron" /><span>任务计划</span>
      <span>{completed}/{items.length}</span>{current && <span className="harness-plan-current">{current.content}</span>}
    </summary>
    {open && <><progress max={Math.max(1, items.length)} value={completed} aria-label="计划完成进度" />
      <ol>{items.map(item => <li key={item.id} data-status={item.status}>
        {item.status === "completed" ? <Check size={13} /> : item.status === "in_progress" ? <CircleAlert size={13} /> : <span aria-hidden="true">○</span>}
        <span>{item.content}</span><span className="activity-status">{item.status === "completed" ? "已完成" : item.status === "in_progress" ? "进行中" : "待处理"}</span>
      </li>)}</ol><p className="harness-tool-note">模型维护的工作计划；验收和验证结果以审计记录为准。</p></>}
  </details>;
}
