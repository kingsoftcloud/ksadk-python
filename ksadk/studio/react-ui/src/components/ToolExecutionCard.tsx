import { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, CircleAlert, FileText, Search, Terminal, Pencil } from "lucide-react";
import { useBackgroundCommandOutput } from "./useBackgroundCommandOutput";

export type ToolDisplay = {
  version: "tool-display/v1";
  category: "terminal" | "read" | "search" | "edit" | "todo" | "tool";
  // Empty optional fields may be omitted by the public API serializer.
  summary?: string; path?: string; output?: string; diff?: string; error_message?: string;
  workspace_path?: string;
  truncated?: boolean; total_chars?: number; exit_code?: number; artifact_id?: string;
  additions?: number; deletions?: number;
  background_status?: "starting" | "running" | "completed" | "failed" | "cancelled" | "timed_out" | "output_limit" | "unknown";
  background_cancel_requested?: boolean;
  background_task_id?: string;
  todos?: { id: string; content: string; status: "pending" | "in_progress" | "completed"; priority: string }[];
};
export type ToolExecution = { id: string; callId: string; name?: string; status: string; display: ToolDisplay };
const names = { terminal: "终端", read: "读取文件", search: "搜索", edit: "文件修改", todo: "更新计划", tool: "工具调用" };
// Historical receipts can lack a display summary. Only fixed builtin labels
// may be recovered from the public tool name; never infer paths or counts.
const builtinActions: Record<string, string> = {
  browser_inspect_workspace: "浏览器检查", list_workspace_files: "查看工作区",
  read_workspace_file: "读取文件", search_workspace_files: "搜索",
  write_workspace_file: "写入", edit_workspace_file: "编辑", multi_edit_workspace_file: "编辑",
  run_command: "终端", read_command_output: "读取后台输出", stop_command: "停止后台命令",
  TodoWrite: "更新计划",
};
const statuses: Record<string, string> = { running: "执行中", paused: "等待处理", completed: "已完成", failed: "失败", unknown: "结果未确认", cancelled: "已取消" };
const backgroundStatuses = { starting: "后台启动中", running: "后台运行", completed: "后台已完成", failed: "后台失败",
  cancelled: "已取消", timed_out: "后台超时", output_limit: "后台输出达上限", unknown: "后台结果未确认" };
const fileKinds: Record<string, string> = { py: "PY", js: "JS", jsx: "JS", ts: "TS", tsx: "TS",
  html: "HTML", css: "CSS", json: "JSON", md: "MD", yaml: "YML", yml: "YML" };

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
  const background = useBackgroundCommandOutput(runId, tool.display.background_task_id);
  const display = background.display ? { ...tool.display, ...background.display, summary: tool.display.summary } : tool.display;
  const summary = display.summary || "";
  const text = display.diff || display.output || "";
  const backgroundStatus = display.background_status;
  const displayStatus = tool.status === "failed" ? "failed"
    : backgroundStatus === "timed_out" || backgroundStatus === "output_limit" ? "failed"
      : backgroundStatus === "starting" ? "running" : backgroundStatus || tool.status;
  const statusLabel = tool.status === "failed" ? statuses.failed : backgroundStatus
    ? display.background_cancel_requested && ["starting", "running"].includes(backgroundStatus)
      ? "正在取消" : backgroundStatuses[backgroundStatus]
    : statuses[tool.status] || "状态未确认";
  useEffect(() => {
    if (following && display.category === "terminal" && outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight;
  }, [text, open, following, display.category]);
  if (display.version !== "tool-display/v1") return null;
  const Icon = display.category === "terminal" ? Terminal : display.category === "search" || display.category === "read" ? Search : display.category === "edit" ? Pencil : FileText;
  const file = ["read", "edit"].includes(display.category) && display.path;
  const extension = file ? display.path!.split(".").at(-1)?.toLowerCase() || "" : "";
  const label = file && summary.endsWith(display.path!)
    ? summary.slice(0, -display.path!.length).replace(/[:：]\s*$/, "").trim() : summary;
  const fileHref = file && runId && display.workspace_path
    ? `/api/v1/runs/${encodeURIComponent(runId)}/documents/content?${new URLSearchParams({ path: display.workspace_path })}` : undefined;
  const parentPath = display.workspace_path?.includes("/")
    ? display.workspace_path.slice(0, display.workspace_path.lastIndexOf("/") + 1) : undefined;
  const terminalSummary = display.category === "terminal" ? summary.replace(/^(?:运行命令|终端)[:：]\s*/, "") : undefined;
  const action = display.category === "edit" && /^write_workspace_file$/.test(tool.name || "") ? "写入"
    : display.category === "edit" && /^edit_workspace_file$/.test(tool.name || "") ? "编辑"
    : tool.name === "read_command_output" ? "读取后台输出" : tool.name === "stop_command" ? "停止后台命令"
      : terminalSummary !== undefined ? "终端" : label || builtinActions[tool.name || ""] || names[display.category];
  return <details className="harness-tool-card" open={open} data-status={displayStatus} data-background-status={backgroundStatus} data-category={display.category} data-call-id={tool.callId}
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary aria-label={`${summary || builtinActions[tool.name || ""] || names[display.category]} · ${statusLabel}${display.error_message ? ` · ${display.error_message}` : ""}`}>
      <Icon size={16} aria-hidden="true" />
      <span className="harness-tool-action">{action}</span>
      {file && (fileKinds[extension] ? <span className="harness-file-type" data-extension={extension}>{fileKinds[extension]}</span> : <FileText size={16} aria-hidden="true" />)}
      {file && (fileHref ? <a className="harness-file-name" href={fileHref} onClick={event => event.stopPropagation()}>{display.path}</a>
        : <span className="harness-file-name">{display.path}</span>)}
      {parentPath && <span className="harness-file-parent" title={display.workspace_path}>{parentPath}</span>}
      {terminalSummary && !["read_command_output", "stop_command"].includes(tool.name || "") && <span className="harness-command-summary" title={terminalSummary}>{terminalSummary}</span>}
      {display.category === "edit" && <span className="harness-change-counts">
        {display.additions !== undefined && display.additions > 0 && <span data-change="add">+{display.additions}</span>}
        {display.deletions !== undefined && display.deletions > 0 && <span data-change="remove">−{display.deletions}</span>}
      </span>}
      {(backgroundStatus || tool.status !== "completed") && <span className="activity-status">{statusLabel}</span>}
      {background.queryFailed && <span className="activity-status">暂未取得最新状态</span>}
      {display.exit_code != null && display.exit_code !== 0 && <span className="activity-status">退出码 {display.exit_code}</span>}
      {display.error_message && <span className="harness-tool-error" title={display.error_message}>{display.error_message}</span>}
      <ChevronRight size={16} className="activity-chevron harness-tool-disclosure" aria-hidden="true" />
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
          {!following && display.category === "terminal" && displayStatus === "running" && <button type="button" onClick={() => setFollowing(true)}>跟随最新输出</button>}
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
      </> : <p>{display.error_message || "未记录可公开展示的输出。完整审计记录可在检查器中查看。"}</p>}
      {display.truncated && <p className="harness-tool-note">输出已截断{display.total_chars ? `（原始 ${display.total_chars.toLocaleString()} 字符）` : ""}，展开高度仅显示已收到的部分。</p>}
      {runId && /^[a-f0-9]{64}$/.test(display.artifact_id || "") && <p className="harness-tool-note">
        <a href={`/api/v1/runs/${encodeURIComponent(runId)}/public-output/${display.artifact_id}`} download>下载已保留输出</a>
        （已脱敏；宿主输出上限之外的内容无法恢复）
      </p>}
    </div>}
  </details>;
}

export type PlanRunState = { status: string; outcome?: string };
export function TaskPlan({ items, runState, previousRun = false }: {
  items: NonNullable<ToolDisplay["todos"]>; runState?: PlanRunState; previousRun?: boolean;
}) {
  const completed = items.filter(item => item.status === "completed").length;
  const current = items.find(item => item.status === "in_progress");
  const ended = ["completed", "failed", "cancelled", "interrupted", "timed_out"].includes(runState?.status.toLowerCase() || "");
  const remaining = items.length - completed;
  const outcomes: Record<string, string> = { verified: "验收通过", unverified: "验收尚未确认", partial: "部分验收通过", blocked: "验收受阻" };
  const outcome = outcomes[runState?.outcome || ""];
  const boundary = ended ? `该轮已结束${remaining ? `，仍有 ${remaining} 项未标记完成` : ""}` : undefined;
  const [open, setOpen] = useState(false);
  return <details className="harness-task-plan" data-run-status={runState?.status} data-ended={ended} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span className="harness-plan-heading"><span>进程</span><span className="harness-plan-count">{completed}/{items.length}</span>
      <ChevronRight size={13} className="activity-chevron" />
      </span><span className="harness-plan-current">{[previousRun ? "前轮计划" : undefined, boundary, outcome,
        !ended && !open ? current?.content : undefined].filter(Boolean).join(" · ")}</span>
    </summary>
    {open && <>{ended && remaining > 0 && <p className="harness-tool-note">计划最后记录尚未全部完成；以下保留原状态，不代表这些事项仍在执行，也不代表完整任务已验收。</p>}
      <progress max={Math.max(1, items.length)} value={completed} aria-label="计划完成进度" />
      <ol>{items.map(item => <li key={item.id} data-status={item.status}>
        {item.status === "completed" ? <Check size={13} /> : item.status === "in_progress" ? <CircleAlert size={13} /> : <span aria-hidden="true">○</span>}
        <span>{item.content}</span><span className="activity-status">{item.status === "completed" ? "已完成" : ended ? item.status === "in_progress" ? "结束前进行中" : "未完成记录" : item.status === "in_progress" ? "进行中" : "待处理"}</span>
      </li>)}</ol></>}
  </details>;
}
