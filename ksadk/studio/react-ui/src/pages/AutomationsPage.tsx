import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CalendarClock, Clock3, Pencil, Play, Plus, Trash2, LayoutGrid, List, Search, RefreshCw, ArrowUpRight } from "lucide-react";
import { apiFetch } from "../api";
import { AutomationEditor } from "./AutomationEditor";
import { Drawer } from "../components/Drawer";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { StudioDataTable, type StudioDataColumn } from "../components/ui/StudioDataTable";

import { type AgentSummary, type ScheduledTask, type ScheduleOccurrence, type SchedulerAvailability, formatTime, taskLabel, triggerLabel, emptyForm, formFromTask, payloadFromForm } from "./automationModel";

function occurrenceState(state: string) {
  if (state === "succeeded") return "成功";
  if (state === "failed") return "失败";
  if (state === "running") return "运行中";
  if (state === "accepted") return "已接收";
  if (state === "claimed") return "已认领";
  if (state === "skipped") return "已跳过";
  if (state === "cancelled") return "已取消";
  return state || "未知";
}

function occurrenceStateTone(state: string) {
  if (state === "succeeded") return "ready";
  if (state === "failed" || state === "cancelled") return "failed";
  return "pending";
}

function occurrenceIsActive(item: ScheduleOccurrence) {
  return item.state === "claimed" || item.state === "accepted" || item.state === "running";
}

const occurrenceDiagnosticLabels: Record<string, string> = {
  DISPATCH_FAILED: "提交失败",
  PLUGIN_EXECUTION_FAILED: "插件执行失败",
  RUNTIME_TIMEOUT: "执行超时",
  misfire_skipped: "错过计划时间，已按策略跳过",
  concurrency_forbid_active_occurrence: "已有执行未结束，本次已跳过",
  runtime_completed: "运行时已确认完成",
};

function diagnosticText(value?: string | null) {
  if (!value) return "";
  return occurrenceDiagnosticLabels[value] || value;
}

function diagnosticTitle(item: ScheduleOccurrence) {
  if (!item.errorCode) return "执行说明";
  const label = diagnosticText(item.errorCode);
  return label === item.errorCode ? label : `${label} · ${item.errorCode}`;
}

function occurrenceTransitions(item: ScheduleOccurrence) {
  if (item.transitions?.length) return item.transitions;
  const inferred = [
    item.claimedAt && { state: "claimed", at: item.claimedAt },
    item.acceptedAt && { state: "accepted", at: item.acceptedAt },
    item.startedAt && { state: "running", at: item.startedAt },
    item.completedAt && { state: item.state, at: item.completedAt, detail: item.detail, errorCode: item.errorCode },
  ];
  return inferred.filter(Boolean) as Array<{ state: string; at: string; detail?: string | null; errorCode?: string | null }>;
}

function OccurrenceHistory({ items, taskNames, agentNames }: {
  items: ScheduleOccurrence[];
  taskNames: Map<string, string>;
  agentNames: Map<string, string>;
}) {
  if (!items.length) return <p className="automation-empty">还没有执行记录。</p>;
  return (
    <div className="automation-occurrences">
      {items.map(item => {
        const agentId = item.target?.agentId || "";
        const transitions = occurrenceTransitions(item);
        return (
          <article key={item.occurrenceId} className="automation-occurrence-card">
            <div className="automation-occurrence-summary">
              <span className="automation-state" data-state={occurrenceStateTone(item.state)}>{occurrenceState(item.state)}</span>
              <strong>{taskNames.get(item.taskId) || item.taskId}</strong>
              <span>{item.trigger === "manual" ? "手动触发" : "计划触发"}</span>
              <time>{formatTime(item.scheduledFor)}</time>
            </div>
            {agentId && item.runId && <a className="button secondary small" href={`#/conversations?agentId=${encodeURIComponent(agentId)}&sessionId=${encodeURIComponent(item.sessionId)}`}><ArrowUpRight size={14} />查看会话与结果</a>}
            <details className="automation-advanced"><summary>执行详情</summary><dl className="automation-occurrence-facts">
              <div><dt>Occurrence</dt><dd>{item.occurrenceId}</dd></div>
              <div><dt>Agent</dt><dd>{agentNames.get(agentId) || agentId || "目标已删除"}</dd></div>
              <div><dt>Attempt</dt><dd>{item.attempt || 1}</dd></div>
              <div><dt>Command</dt><dd>{item.commandId || "尚未接收"}</dd></div>
              <div><dt>Session</dt><dd>{item.sessionId}</dd></div>
              <div><dt>Run</dt><dd>{item.runId || "尚未绑定"}</dd></div>
            </dl></details>
            {transitions.length > 0 && (
              <ol className="automation-timeline" aria-label={`${item.occurrenceId} 状态时间线`}>
                {transitions.map((transition, index) => (
                  <li key={`${transition.state}-${transition.at}-${index}`}>
                    <span>{occurrenceState(transition.state)}</span>
                    <time>{formatTime(transition.at)}</time>
                    {(transition.errorCode || transition.detail) && <small>{diagnosticText(transition.errorCode || transition.detail)}</small>}
                  </li>
                ))}
              </ol>
            )}
            {(item.errorCode || item.detail) && <div className="automation-diagnosis"><strong>{diagnosticTitle(item)}</strong><span>{diagnosticText(item.detail) || "—"}</span></div>}
          </article>
        );
      })}
    </div>
  );
}

export function AutomationsPage({ currentAgentId, agents, scopedAgentId = "", embedded = false, onTaskCountChanged }: {
  currentAgentId: string;
  agents: AgentSummary[];
  onSelectAgent: (agentId: string) => void;
  scopedAgentId?: string;
  embedded?: boolean;
  onTaskCountChanged?: (count: number) => void;
}) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [allOccurrences, setAllOccurrences] = useState<ScheduleOccurrence[]>([]);
  const [taskOccurrences, setTaskOccurrences] = useState<ScheduleOccurrence[]>([]);
  const [availability, setAvailability] = useState<SchedulerAvailability | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ScheduledTask | null>(null);
  const [occurrences, setOccurrences] = useState<ScheduleOccurrence[]>([]);
  const [form, setForm] = useState(() => emptyForm(currentAgentId));
  const [editingTaskId, setEditingTaskId] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [deleteTask, setDeleteTask] = useState<ScheduledTask | null>(null);
  const [section, setSection] = useState<"tasks" | "history">("tasks");
  const [viewMode, setViewMode] = useState<"board" | "list">("board");
  const [query, setQuery] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const occurrenceRequest = useRef(0);
  const taskRequest = useRef(0);
  const [agentFilter, setAgentFilter] = useState(scopedAgentId);

  const loadTasks = useCallback(async (quiet = false) => {
    const requestId = ++taskRequest.current;
    if (!quiet) setLoading(true);
    try {
      const [response, historyResponse] = await Promise.all([
        apiFetch("/api/v1/schedules"),
        apiFetch("/api/v1/schedule-occurrences?limit=200"),
      ]);
      if (!response.ok) throw new Error(`定时任务加载失败（${response.status}）`);
      const payload = await response.json();
      if (!historyResponse.ok) throw new Error("执行记录加载失败，请刷新重试");
      const historyPayload = await historyResponse.json();
      if (requestId !== taskRequest.current) return;
      const items = (payload.items || []) as ScheduledTask[];
      setTasks(items);
      setAllOccurrences(historyPayload.items || []);
      setTaskOccurrences(payload.taskOccurrences || historyPayload.items || []);
      setAvailability(payload.availability || null);
      setSelected(previous => items.find(item => item.taskId === previous?.taskId) || null);
      if (onTaskCountChanged) {
        onTaskCountChanged(scopedAgentId
          ? items.filter(item => item.target.agentId === scopedAgentId).length
          : items.length);
      }
    } catch (loadError: any) {
      if (requestId === taskRequest.current) setError(loadError?.message || "定时任务加载失败");
    } finally {
      if (requestId === taskRequest.current) setLoading(false);
    }
  }, [onTaskCountChanged, scopedAgentId]);

  const loadOccurrences = useCallback(async (task: ScheduledTask, clear = true) => {
    const requestId = ++occurrenceRequest.current;
    if (clear) { setSelected(task); setOccurrences([]); }
    try {
      const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(task.target.agentId || "")}/schedules/${encodeURIComponent(task.taskId)}/occurrences`);
      if (!response.ok) throw new Error("运行记录加载失败，请重新打开任务。");
      const payload = await response.json();
      if (requestId === occurrenceRequest.current) setOccurrences(payload.items || []);
    } catch (error) { if (requestId === occurrenceRequest.current) setError((error as Error).message); }
  }, []);

  useEffect(() => {
    void loadTasks();
    const timer = window.setInterval(() => { if (!document.hidden) void loadTasks(true); }, 5000);
    return () => { window.clearInterval(timer); taskRequest.current++; occurrenceRequest.current++; };
  }, [loadTasks]);
  useEffect(() => { setAgentFilter(scopedAgentId); setSelected(null); occurrenceRequest.current++; }, [scopedAgentId]);
  useEffect(() => {
    if (!selected) return;
    const timer = window.setInterval(() => { if (!document.hidden) void loadOccurrences(selected, false); }, 2000);
    return () => window.clearInterval(timer);
  }, [loadOccurrences, selected?.taskId]);

  const agentName = useMemo(
    () => new Map(agents.map(agent => [agent.metadata.id, agent.metadata.name])),
    [agents],
  );
  const taskName = useMemo(
    () => new Map(tasks.map(task => [task.taskId, taskLabel(task)])),
    [tasks],
  );
  const filteredTasks = useMemo(
    () => tasks.filter(task => (!agentFilter || task.target.agentId === agentFilter) && `${taskLabel(task)} ${task.command.payload.content || ""}`.toLowerCase().includes(query.toLowerCase())),
    [agentFilter, tasks, query],
  );
  const filteredOccurrences = useMemo(
    () => agentFilter ? allOccurrences.filter(item => item.target?.agentId === agentFilter) : allOccurrences,
    [agentFilter, allOccurrences],
  );
  const latestByTask = useMemo(() => {
    const latest = new Map<string, ScheduleOccurrence>();
    for (const item of taskOccurrences) if (!latest.has(item.taskId)) latest.set(item.taskId, item);
    return latest;
  }, [taskOccurrences]);

  const columns: StudioDataColumn<ScheduledTask>[] = [
    {
      id: "task", header: "任务", minWidth: 220,
      cell: task => <div className="automation-task-cell"><strong>{taskLabel(task)}</strong><span>{agentName.get(task.target.agentId || "") || task.target.agentId || "历史任务"}</span></div>,
    },
    { id: "trigger", header: "触发规则", minWidth: 150, cell: task => triggerLabel(task.schedule) },
    { id: "next", header: "下次执行", minWidth: 120, cell: task => task.enabled ? formatTime(task.nextRunAt) : "—" },
    { id: "mode", header: "会话", minWidth: 100, cell: task => task.continuity === "continue_session" ? "继续会话" : "新会话" },
    { id: "recent", header: "最近终态", minWidth: 110, cell: task => latestByTask.has(task.taskId) ? occurrenceState(latestByTask.get(task.taskId)!.state) : "—" },
    { id: "state", header: "状态", width: 110, cell: task => {
      const label = !task.enabled ? "已停用" : availability?.triggerActive ? "已启用" : "已配置";
      return <span className="automation-state" data-state={task.enabled && availability?.triggerActive ? "ready" : "idle"}>{label}</span>;
    } },
  ];

  function startEdit(task: ScheduledTask) {
    setEditingTaskId(task.taskId);
    setForm(formFromTask(task));
    setSelected(task);
    setEditorOpen(true);
    setSection("tasks");
    void loadOccurrences(task);
  }

  async function save() {
    if (!form.agentId) { setError("请选择 Agent"); return; }
    if (!form.prompt.trim()) { setError("请填写到期时发送给 Agent 的提示词"); return; }
    setSubmitting(true);
    setError("");
    try {
      const body = payloadFromForm(form);
      const isEditing = Boolean(editingTaskId);
      const path = isEditing
        ? `/api/v1/agents/${encodeURIComponent(form.agentId)}/schedules/${encodeURIComponent(editingTaskId)}`
        : `/api/v1/agents/${encodeURIComponent(form.agentId)}/schedules`;
      const response = await apiFetch(path, {
        method: isEditing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.error?.message || `保存失败（${response.status}）`);
      setEditingTaskId("");
      setEditorOpen(false);
      setForm(emptyForm(form.agentId));
      showToast(isEditing ? "定时任务已更新" : "定时任务已创建", payload?.displayName || "本地自动化");
      await loadTasks();
      if (payload) await loadOccurrences(payload as ScheduledTask);
    } catch (saveError: any) {
      setError(saveError?.message || "保存定时任务失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function runNow(task: ScheduledTask) {
    const agentId = task.target.agentId;
    if (!agentId) { setError("历史任务缺少 Agent 标识，不能从 Studio 触发"); return; }
    const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/schedules/${encodeURIComponent(task.taskId)}:run`, { method: "POST" });
    const payload = await response.json().catch(() => null);
    if (!response.ok) { setError(payload?.error?.message || `立即运行失败（${response.status}）`); return; }
    showToast("任务已提交", "可在执行记录中查看进度和结果。");
    await loadOccurrences(task);
    await loadTasks();
  }

  async function toggle(task: ScheduledTask) {
    const agentId = task.target.agentId;
    if (!agentId) return;
    const update = { displayName: taskLabel(task), prompt: task.command.payload.content, schedule: task.schedule, enabled: !task.enabled, continuity: task.continuity, sessionId: task.target.sessionId || null };
    const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/schedules/${encodeURIComponent(task.taskId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(update),
    });
    if (!response.ok) { setError("更新任务状态失败"); return; }
    await loadTasks();
  }

  async function confirmDelete() {
    if (!deleteTask?.target.agentId) return;
    const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(deleteTask.target.agentId)}/schedules/${encodeURIComponent(deleteTask.taskId)}`, { method: "DELETE" });
    if (!response.ok) { setError("删除任务失败"); return; }
    setDeleteTask(null);
    setSelected(null);
    setOccurrences([]);
    await loadTasks();
  }

  function startCreate(template?: "daily" | "interval" | "weekly") {
    setError("");
    setSelected(null);
    setEditingTaskId("");
    const draft = emptyForm(scopedAgentId || agentFilter || currentAgentId || agents[0]?.metadata.id || "");
    if (template === "daily") { draft.displayName = "每日工作简报"; draft.prompt = "整理昨日工作进展，列出今日待办和需要关注的风险。"; }
    if (template === "interval") { draft.displayName = "定期巡检"; draft.prompt = "检查当前服务状态，汇总异常和建议处理的事项。"; draft.kind = "interval"; }
    if (template === "weekly") { draft.displayName = "每周总结"; draft.prompt = "总结本周完成的工作、未完成事项和下周计划。"; draft.preset = "weekly"; draft.weekday = "5"; draft.time = "17:00"; }
    setForm(draft); setEditorOpen(true);
  }
  async function perform(action: () => Promise<void>) {
    if (actionBusy) return;
    setActionBusy(true); setError("");
    try { await action(); } catch (error) { setError((error as Error).message || "操作失败，请重试"); }
    finally { setActionBusy(false); }
  }
  function status(task: ScheduledTask) {
    const latest = latestByTask.get(task.taskId);
    if (latest && occurrenceIsActive(latest)) return "running";
    if (!task.enabled) return "paused";
    if (latest?.state === "failed") return "attention";
    return "scheduled";
  }
  const groups = [{ id: "scheduled", label: "待执行" }, { id: "running", label: "执行中" }, { id: "attention", label: "需关注" }, { id: "paused", label: "已暂停 / 已结束" }];
  const createButton = <button className="button accent" type="button" onClick={() => startCreate()}><Plus size={15} />新建定时任务</button>;
  const closeDetail = () => { occurrenceRequest.current++; setSelected(null); setOccurrences([]); };

  return (
    <div className={`${embedded ? "automation-page-embedded" : "page-container"} automation-page`} data-layout={embedded ? "embedded" : "document"}>
      {!embedded && <PageHeaderActions>{createButton}</PageHeaderActions>}
      <div className="automation-intro">
        <div><h2>{embedded ? "该 Agent 的自动化" : "让重复的工作，按时完成"}</h2><p>安排任务、跟进进度，在每次执行后查看结果。</p></div>
        {embedded && createButton}
      </div>
      {error && !editorOpen && !selected && <div className="form-error" role="alert">{error}</div>}
      <div className="automation-summary-strip">
        <span className="automation-availability" data-state={availability?.triggerActive ? "ready" : "idle"}>{availability?.triggerActive ? "本地调度运行中" : "本地调度未就绪"}</span>
        <span>保持 Studio 运行，任务才会自动执行。</span>
        <button type="button" className="button secondary small" aria-label="刷新任务" onClick={() => { setError(""); void loadTasks(); }}><RefreshCw size={14} /></button>
      </div>
      <div className="automation-templates"><span>从常用任务开始</span><button onClick={() => startCreate("daily")}><CalendarClock size={15} />每日简报<small>每天 10:00</small></button><button onClick={() => startCreate("interval")}><Clock3 size={15} />定期巡检<small>每 30 分钟</small></button><button onClick={() => startCreate("weekly")}><CalendarClock size={15} />每周总结<small>周五 17:00</small></button></div>
      <div className="automation-toolbar">
        <div className="automation-tabs" role="tablist" aria-label="自动化视图">
          <button role="tab" aria-selected={section === "tasks"} className={section === "tasks" ? "active" : ""} onClick={() => setSection("tasks")}>全部任务 <small>{filteredTasks.length}</small></button>
          <button role="tab" aria-selected={section === "history"} className={section === "history" ? "active" : ""} onClick={() => setSection("history")}>执行记录</button>
        </div>
        <div className="automation-filters">
          {section === "tasks" && <label className="automation-search"><Search size={15} /><input aria-label="搜索任务" placeholder="搜索任务" value={query} onChange={event => setQuery(event.target.value)} /></label>}
          {!embedded && <select aria-label="筛选 Agent" value={agentFilter} onChange={event => setAgentFilter(event.target.value)}><option value="">全部智能体</option>{agents.map(agent => <option key={agent.metadata.id} value={agent.metadata.id}>{agent.metadata.name}</option>)}</select>}
          {section === "tasks" && <div className="automation-view-toggle" role="group" aria-label="任务布局"><button aria-label="看板视图" aria-pressed={viewMode === "board"} onClick={() => setViewMode("board")}><LayoutGrid size={16} /></button><button aria-label="列表视图" aria-pressed={viewMode === "list"} onClick={() => setViewMode("list")}><List size={16} /></button></div>}
        </div>
      </div>
      {section === "tasks" && (viewMode === "list" ? <StudioDataTable columns={columns} data={filteredTasks} getRowId={task => task.taskId} caption="定时任务列表" loading={loading} error={error && !tasks.length ? error : ""} onRetry={() => void loadTasks()} onRowActivate={task => void loadOccurrences(task)} rowAriaLabel={task => `查看定时任务 ${taskLabel(task)} 的详情`} empty={{ icon: <CalendarClock size={22} />, title: "还没有定时任务", description: "新建任务，或在对话中说：每天10点帮我生成日报。" }} /> :
        <div className="automation-board" aria-label="任务看板" aria-busy={loading}>
          {groups.map(group => {
            const items = filteredTasks.filter(task => status(task) === group.id);
            return <section key={group.id} className="automation-board-column" data-status={group.id} aria-label={group.label}>
              <header><span className="automation-column-dot" /><h3>{group.label}</h3><span>{items.length}</span></header>
              {items.map(task => {
                const latest = latestByTask.get(task.taskId);
                return <button key={task.taskId} className="automation-board-card" aria-label={`查看定时任务 ${taskLabel(task)} 的详情`} onClick={() => void loadOccurrences(task)}>
                  <span className="automation-card-agent">{agentName.get(task.target.agentId || "") || "智能体"}</span>
                  <strong>{taskLabel(task)}</strong><p>{task.command.payload.content}</p>
                  <span className="automation-card-rule"><CalendarClock size={14} />{triggerLabel(task.schedule)}</span>
                  <footer><span>{task.enabled ? `下次 ${formatTime(task.nextRunAt)}` : task.schedule.kind === "once" && latest ? "已结束" : "已暂停"}</span>{latest && <span data-state={latest.state}>{occurrenceState(latest.state)}</span>}</footer>
                </button>;
              })}
              {!items.length && <p className="automation-column-empty">{loading ? "正在加载…" : query ? "没有匹配的任务" : group.id === "scheduled" ? "新建任务，安排下一次执行" : "暂无任务"}</p>}
              {group.id === "scheduled" && <button className="automation-column-add" onClick={() => startCreate()}><Plus size={14} />添加任务</button>}
            </section>;
          })}
        </div>)}
      {section === "history" && <section className="automation-history block"><div className="section-heading"><div className="section-heading-copy"><h2>执行记录</h2><p>查看每次任务的状态，打开会话阅读结果。</p></div></div><OccurrenceHistory items={filteredOccurrences} taskNames={taskName} agentNames={agentName} /></section>}
      <p className="automation-conversation-tip">也可以在智能体对话中直接说：<strong>“每天 10 点帮我生成日报”</strong>，或 <strong>“每 30 分钟检查一次服务状态”</strong>。</p>
      {editorOpen && <AutomationEditor form={form} agents={agents} editing={Boolean(editingTaskId)} scoped={Boolean(scopedAgentId)} busy={submitting} error={error} onChange={setForm} onSave={() => void save()} onClose={() => { setEditorOpen(false); setEditingTaskId(""); setError(""); }} />}
      {selected && !editorOpen && <Drawer title={taskLabel(selected)} subtitle={`${agentName.get(selected.target.agentId || "") || "智能体"} · ${triggerLabel(selected.schedule)}`} onClose={closeDetail}>
        <div className="automation-detail">
          {error && <div className="form-error" role="alert">{error}</div>}
          <div className="automation-detail-heading"><span className="automation-state" data-state={selected.enabled ? "ready" : "idle"}>{selected.enabled ? "已启用" : "已暂停"}</span><span>下次执行 {selected.enabled ? formatTime(selected.nextRunAt) : "—"}</span></div>
          <div className="automation-detail-actions"><button className="button secondary small" disabled={actionBusy} onClick={() => startEdit(selected)}><Pencil size={14} />编辑</button><button className="button secondary small" disabled={actionBusy || !availability?.available || !selected.enabled || occurrences.some(occurrenceIsActive)} onClick={() => void perform(() => runNow(selected))}><Play size={14} />立即运行</button><button className="button secondary small" disabled={actionBusy} onClick={() => void perform(() => toggle(selected))}>{selected.enabled ? "暂停" : "启用"}</button></div>
          <div className="automation-detail-grid"><div><span>任务说明</span><p>{selected.command.payload.content}</p></div><div><span>执行计划</span><p>{triggerLabel(selected.schedule)} · {selected.schedule.timezone}</p></div><div><span>对话方式</span><p>{selected.continuity === "continue_session" ? "继续已有会话" : "每次新建会话"}</p></div><div><span>错过时</span><p>{selected.schedule.misfirePolicy === "run_once" ? "补跑一次" : "等待下一次"}</p></div></div>
          <div className="automation-inspector-history"><h3><Clock3 size={16} />最近执行</h3><OccurrenceHistory items={occurrences} taskNames={taskName} agentNames={agentName} /></div>
          <details className="automation-advanced"><summary>技术详情</summary><p>任务：{selected.taskId}</p><p>构建版本：{selected.target.agentVersionRef}</p></details>
          <button className="automation-delete-link" disabled={actionBusy} onClick={() => setDeleteTask(selected)}><Trash2 size={14} />删除任务</button>
        </div>
      </Drawer>}
      {deleteTask && <ConfirmDialog title={`删除定时任务「${taskLabel(deleteTask)}」？`} description="删除后不再自动执行，已有执行记录会保留。" confirmText="删除任务" busy={actionBusy} onConfirm={() => void perform(confirmDelete)} onCancel={() => setDeleteTask(null)} />}
    </div>
  );
}
