import { useEffect, useRef, useState } from "react";
import { ChildControlError, type ChildInput, type ChildRunControls, type ChildTask } from "../childRunControl";
import type { ControlRun } from "../runControlAdmission";
import "./childRunControls.css";

const phases: Record<string, string> = {
  created: "准备启动", preparing_context: "整理上下文", requesting_model: "处理中",
  executing_tools: "执行工具", awaiting_interaction: "等待确认", compacting: "压缩上下文",
  verifying: "验证中", finalizing: "整理结果", paused: "已暂停", completed: "已结束",
  failed: "执行失败", canceled: "已取消", interrupted: "已中断",
};

function ChildRow({ task, run, api, allowInput, allowPromotion, allowCancellation, refresh }: {
  task: ChildTask; run: ControlRun; api: ChildRunControls;
  allowInput: boolean; allowPromotion: boolean; allowCancellation: boolean; refresh: () => void;
}) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [cancellationQueued, setCancellationQueued] = useState(false);
  const [promotionQueued, setPromotionQueued] = useState(false);
  const attempt = useRef<{ action: "send"; input: ChildInput } | { action: "promote" | "cancel"; commandId: string } | null>(null);
  const inFlight = useRef(false);
  const alive = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const perform = async (action: "send" | "promote" | "cancel") => {
    if (inFlight.current || (attempt.current && attempt.current.action !== action)) return;
    if (!attempt.current) {
      if (action === "send" && (!task.can_send_input || !allowInput || !text.trim())) return;
      if (action === "promote" && (!task.can_promote || !allowPromotion || promotionQueued)) return;
      if (action === "cancel" && (!task.can_cancel || !allowCancellation || !api.cancelChild || cancellationQueued)) return;
      const commandId = `studio-child:${crypto.randomUUID()}`;
      attempt.current = action === "send" ? { action, input: {
        command_id: commandId, text: text.trim(), expected_goal_revision: task.goal_revision,
      } } : { action, commandId };
    }
    const current = attempt.current;
    inFlight.current = true; setPending(true); setNotice("");
    try {
      if (current.action === "send") await api.sendChildInput(run, task.child_run_id, current.input);
      else if (current.action === "cancel") await api.cancelChild!(run, task.child_run_id, current.commandId);
      else await api.promoteChild(run, task.child_run_id, current.commandId);
      if (!alive.current) return;
      attempt.current = null; setUncertain(false);
      if (current.action === "send") {
        setText(""); setNotice("补充已排队，等待子任务处理；不会自动批准或恢复暂停。");
      } else if (current.action === "cancel") {
        setCancellationQueued(true); setNotice("取消请求已排队，最终状态以运行时为准。");
      } else {
        setPromotionQueued(true); setNotice("转后台请求已排队，当前状态以运行时返回为准。");
      }
      refresh();
    } catch (error) {
      if (!alive.current) return;
      // Unknown outcomes retain the exact original request for explicit retry.
      const unknown = !(error instanceof ChildControlError) || error.uncertain;
      if (!unknown) attempt.current = null;
      setUncertain(unknown);
      setNotice(error instanceof Error ? error.message : "请求未能确认，请用原请求重试。");
    } finally {
      if (alive.current) { inFlight.current = false; setPending(false); }
    }
  };
  return <article className="child-run-row" aria-label={`${task.role || "子任务"} · ${task.child_run_id}`}>
    <div className="child-run-heading">
      <strong>{task.role || "子任务"}</strong>
      <span>{task.delivery === "foreground" ? "前台" : "后台"} · {phases[task.phase] || "状态待确认"}</span>
    </div>
    {allowInput && task.can_send_input ? <div className="child-run-input">
      <input aria-label={`给${task.role || "子任务"}补充说明`} value={text} maxLength={32768}
        readOnly={uncertain} disabled={pending} placeholder="补充说明，保留当前任务和权限"
        onChange={event => setText(event.target.value)}
        onKeyDown={event => {
          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault(); event.stopPropagation(); void perform("send");
          }
        }} />
      <button type="button" disabled={pending || !text.trim() || (uncertain && attempt.current?.action !== "send")}
        onClick={() => { void perform("send"); }}>{uncertain && attempt.current?.action === "send" ? "重试原补充" : "发送补充"}</button>
    </div> : null}
    {allowPromotion && task.can_promote ? <button type="button"
      disabled={pending || promotionQueued || (uncertain && attempt.current?.action !== "promote")}
      onClick={() => { void perform("promote"); }}>{uncertain && attempt.current?.action === "promote" ? "重试转后台" : "转到后台"}</button> : null}
    {allowCancellation && task.can_cancel && api.cancelChild ? <button type="button"
      disabled={pending || cancellationQueued || (uncertain && attempt.current?.action !== "cancel")}
      onClick={() => { void perform("cancel"); }}>{uncertain && attempt.current?.action === "cancel" ? "重试原取消" : "取消此子任务"}</button> : null}
    {notice ? <p role="status">{notice}</p> : null}
  </article>;
}

/** Disposable view of the original child directory; no optimistic run state. */
export function ChildRunControls({ run, api, allowInput, allowPromotion, allowCancellation = false }: {
  run: ControlRun; api: ChildRunControls; allowInput: boolean; allowPromotion: boolean; allowCancellation?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<ChildTask[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      setLoading(true);
      try {
        const next = await api.childTasks(run, controller.signal);
        if (controller.signal.aborted) return;
        setTasks(next); setError("");
        timer = setTimeout(() => { void read(); }, 2000);
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(failure instanceof Error ? failure.message : "子任务状态读取失败，请刷新。");
        }
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [open, api, run.agentId, run.sessionId, run.invocationId, run.runId, revision]);
  return <section className="child-run-controls" aria-label="子任务控制">
    <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>子任务</button>
    <div hidden={!open}>
      {error ? <p role="alert">{error}</p> : null}
      <button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>刷新子任务</button>
      {!loading && !error && tasks.length === 0 ? <p>当前没有子任务。</p> : null}
      {tasks.map(task => <ChildRow key={`${run.runId}:${task.child_run_id}`} task={task} run={run} api={api}
        allowInput={allowInput && !error} allowPromotion={allowPromotion && !error} allowCancellation={allowCancellation && !error} refresh={() => setRevision(value => value + 1)} />)}
    </div>
  </section>;
}
