import { useCallback, useEffect, useMemo, useState } from "react";
import { CalendarClock, Clock3, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { apiFetch } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { StudioDataTable, type StudioDataColumn } from "../components/ui/StudioDataTable";

interface AgentSummary {
  metadata: { id: string; name: string };
}

interface ScheduleSpec {
  kind: "once" | "interval" | "cron";
  timezone: string;
  at?: string | null;
  everySeconds?: number | null;
  expression?: string | null;
  misfirePolicy?: "skip" | "run_once";
}

interface ScheduledTask {
  taskId: string;
  displayName?: string | null;
  target: { agentId?: string | null; agentVersionRef?: string | null; sessionId?: string | null };
  schedule: ScheduleSpec;
  command: { payload: { content?: string } };
  enabled: boolean;
  continuity: "new_session" | "continue_session";
  nextRunAt?: string | null;
}

interface ScheduleOccurrence {
  occurrenceId: string;
  taskId: string;
  target?: { agentId?: string | null; agentVersionRef?: string | null } | null;
  scheduledFor: string;
  trigger: "schedule" | "manual";
  state: string;
  attempt: number;
  commandId?: string | null;
  sessionId: string;
  runId?: string | null;
  claimedAt?: string | null;
  acceptedAt?: string | null;
  startedAt?: string | null;
  detail?: string | null;
  errorCode?: string | null;
  completedAt?: string | null;
  transitions?: Array<{ state: string; at: string; detail?: string | null; errorCode?: string | null }>;
}

interface SchedulerAvailability {
  available?: boolean;
  triggerActive?: boolean;
  running?: boolean;
  ownerId?: string;
  store?: string;
  nextScanAt?: string | null;
  lastScanAt?: string | null;
  lastScanResult?: string | null;
  lastScanDetail?: string | null;
  reason?: string | null;
}

interface ScheduleForm {
  agentId: string;
  displayName: string;
  prompt: string;
  kind: ScheduleSpec["kind"];
  timezone: string;
  at: string;
  everySeconds: string;
  expression: string;
  misfirePolicy: "skip" | "run_once";
  enabled: boolean;
  continuity: "new_session" | "continue_session";
  sessionId: string;
}

function formatTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function taskLabel(task: ScheduledTask) {
  return task.displayName || task.command.payload.content?.slice(0, 28) || task.taskId;
}

function triggerLabel(schedule: ScheduleSpec) {
  if (schedule.kind === "once") return `单次 · ${formatTime(schedule.at)}`;
  if (schedule.kind === "interval") return `每 ${schedule.everySeconds || 60} 秒`;
  return schedule.expression || "Cron";
}

function emptyForm(agentId = ""): ScheduleForm {
  return {
    agentId,
    displayName: "",
    prompt: "",
    kind: "cron",
    timezone: "Asia/Shanghai",
    at: "",
    everySeconds: "3600",
    expression: "0 9 * * 1-5",
    misfirePolicy: "run_once",
    enabled: true,
    continuity: "new_session",
    sessionId: "",
  };
}

function formFromTask(task: ScheduledTask): ScheduleForm {
  return {
    ...emptyForm(task.target.agentId || ""),
    agentId: task.target.agentId || "",
    displayName: task.displayName || "",
    prompt: task.command.payload.content || "",
    kind: task.schedule.kind,
    timezone: task.schedule.timezone || "Asia/Shanghai",
    at: task.schedule.at ? task.schedule.at.slice(0, 16) : "",
    everySeconds: String(task.schedule.everySeconds || 3600),
    expression: task.schedule.expression || "0 9 * * 1-5",
    misfirePolicy: task.schedule.misfirePolicy || "skip",
    enabled: task.enabled,
    continuity: task.continuity,
    sessionId: task.target.sessionId || "",
  };
}

function payloadFromForm(form: ScheduleForm) {
  const schedule: ScheduleSpec = {
    kind: form.kind,
    timezone: form.timezone.trim() || "Asia/Shanghai",
    misfirePolicy: form.misfirePolicy,
  };
  if (form.kind === "once") {
    if (!form.at) throw new Error("请填写单次任务的执行时间");
    schedule.at = new Date(form.at).toISOString();
  } else if (form.kind === "interval") {
    const seconds = Number(form.everySeconds);
    if (!Number.isInteger(seconds) || seconds < 60) throw new Error("间隔至少为 60 秒");
    schedule.everySeconds = seconds;
  } else {
    if (!form.expression.trim()) throw new Error("请填写 Cron 表达式");
    schedule.expression = form.expression.trim();
  }
  return {
    displayName: form.displayName.trim() || form.prompt.trim().slice(0, 32),
    prompt: form.prompt.trim(),
    schedule,
    enabled: form.enabled,
    continuity: form.continuity,
    sessionId: form.continuity === "continue_session" ? form.sessionId.trim() || null : null,
  };
}

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
            <dl className="automation-occurrence-facts">
              <div><dt>Occurrence</dt><dd>{item.occurrenceId}</dd></div>
              <div><dt>Agent</dt><dd>{agentNames.get(agentId) || agentId || "目标已删除"}</dd></div>
              <div><dt>Attempt</dt><dd>{item.attempt || 1}</dd></div>
              <div><dt>Command</dt><dd>{item.commandId || "尚未接收"}</dd></div>
              <div><dt>Session</dt><dd>{item.sessionId}</dd></div>
              <div><dt>Run</dt><dd>{item.runId || "尚未绑定"}</dd></div>
            </dl>
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

export function AutomationsPage({ currentAgentId, agents, onSelectAgent, scopedAgentId = "", embedded = false, onTaskCountChanged }: {
  currentAgentId: string;
  agents: AgentSummary[];
  onSelectAgent: (agentId: string) => void;
  scopedAgentId?: string;
  embedded?: boolean;
  onTaskCountChanged?: (count: number) => void;
}) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [allOccurrences, setAllOccurrences] = useState<ScheduleOccurrence[]>([]);
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
  const [agentFilter, setAgentFilter] = useState(scopedAgentId);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [response, historyResponse] = await Promise.all([
        apiFetch("/api/v1/schedules"),
        apiFetch("/api/v1/schedule-occurrences?limit=200"),
      ]);
      if (!response.ok) throw new Error(`定时任务加载失败（${response.status}）`);
      const payload = await response.json();
      const historyPayload = historyResponse.ok ? await historyResponse.json() : { items: [] };
      const items = (payload.items || []) as ScheduledTask[];
      setTasks(items);
      setAllOccurrences(historyPayload.items || []);
      setAvailability(payload.availability || null);
      setSelected(previous => items.find(item => item.taskId === previous?.taskId) || null);
      if (onTaskCountChanged) {
        onTaskCountChanged(scopedAgentId
          ? items.filter(item => item.target.agentId === scopedAgentId).length
          : items.length);
      }
    } catch (loadError: any) {
      setError(loadError?.message || "定时任务加载失败");
    } finally {
      setLoading(false);
    }
  }, [onTaskCountChanged, scopedAgentId]);

  const loadOccurrences = useCallback(async (task: ScheduledTask, clear = true) => {
    setSelected(task);
    if (clear) setOccurrences([]);
    const agentId = task.target.agentId || "";
    if (!agentId) return [];
    const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/schedules/${encodeURIComponent(task.taskId)}/occurrences`);
    if (!response.ok) return [];
    const payload = await response.json();
    const items = (payload.items || []) as ScheduleOccurrence[];
    setOccurrences(items);
    return items;
  }, []);

  useEffect(() => { void loadTasks(); }, [loadTasks]);
  useEffect(() => { setAgentFilter(scopedAgentId); }, [scopedAgentId]);
  useEffect(() => {
    if (!editingTaskId) setForm(emptyForm(currentAgentId));
  }, [currentAgentId, editingTaskId]);
  const occurrenceActive = occurrences.some(occurrenceIsActive);
  useEffect(() => {
    if (!selected || !occurrenceActive) return undefined;
    const timer = window.setInterval(() => {
      void loadOccurrences(selected, false).then(items => {
        if (!items.some(occurrenceIsActive)) void loadTasks();
      });
    }, 750);
    return () => window.clearInterval(timer);
  }, [loadOccurrences, loadTasks, occurrenceActive, selected]);

  const agentName = useMemo(
    () => new Map(agents.map(agent => [agent.metadata.id, agent.metadata.name])),
    [agents],
  );
  const taskName = useMemo(
    () => new Map(tasks.map(task => [task.taskId, taskLabel(task)])),
    [tasks],
  );
  const filteredTasks = useMemo(
    () => agentFilter ? tasks.filter(task => task.target.agentId === agentFilter) : tasks,
    [agentFilter, tasks],
  );
  const filteredOccurrences = useMemo(
    () => agentFilter ? allOccurrences.filter(item => item.target?.agentId === agentFilter) : allOccurrences,
    [agentFilter, allOccurrences],
  );
  const latestByTask = useMemo(() => {
    const latest = new Map<string, ScheduleOccurrence>();
    for (const item of allOccurrences) if (!latest.has(item.taskId)) latest.set(item.taskId, item);
    return latest;
  }, [allOccurrences]);

  const columns: StudioDataColumn<ScheduledTask>[] = [
    {
      id: "task", header: "任务", minWidth: 220,
      cell: task => <div className="automation-task-cell"><strong>{taskLabel(task)}</strong><span>{agentName.get(task.target.agentId || "") || task.target.agentId || "历史任务"}</span></div>,
    },
    { id: "trigger", header: "触发规则", minWidth: 150, cell: task => triggerLabel(task.schedule) },
    { id: "next", header: "下次执行", minWidth: 120, cell: task => formatTime(task.nextRunAt) },
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
      if (isEditing && payload) await loadOccurrences(payload as ScheduledTask);
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
    showToast("已提交到本地 Agent Kernel", "等待终态对账，不以 accepted 当作成功。");
    await loadOccurrences(task);
    await loadTasks();
  }

  async function toggle(task: ScheduledTask) {
    const agentId = task.target.agentId;
    if (!agentId) return;
    const update = {
      ...formFromTask(task),
      enabled: !task.enabled,
    };
    const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/schedules/${encodeURIComponent(task.taskId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payloadFromForm(update)),
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

  return (
    <div className={`${embedded ? "automation-page-embedded" : "page-container"} automation-page`} data-layout={embedded ? "embedded" : "document"}>
      {!embedded && <PageHeaderActions>
        <button className="button accent" type="button" onClick={() => { setEditingTaskId(""); setForm(emptyForm(currentAgentId)); setEditorOpen(true); }}>
          <Plus size={15} />新建定时任务
        </button>
      </PageHeaderActions>}
      <div className="automation-intro">
        <div><h2>{embedded ? "该 Agent 的自动化" : "自动化 / 定时任务"}</h2><p>在已精确绑定的 Agent Kernel 上运行。选择一条任务查看详情，只有新建或编辑时才打开表单。</p></div>
        <span className="automation-availability" data-state={availability?.triggerActive ? "ready" : "idle"}>
          {availability?.triggerActive ? "本地调度运行中" : availability?.running ? "监视中 · 等待 Runtime" : "本地调度未启动"}
        </span>
      </div>
      {error && <div className="form-error">{error}</div>}
      <div className="automation-runtime-boundary" role="status">
        <div><span>执行环境</span><strong>本地 Agent Kernel</strong></div>
        <div><span>调度状态</span><strong>{availability?.triggerActive ? "运行中" : availability?.running ? "等待 Runtime" : "未启动"}</strong></div>
        <div><span>下次扫描</span><strong>{formatTime(availability?.nextScanAt)}</strong></div>
      </div>
      <div className="automation-toolbar">
        <div className="automation-tabs" role="tablist" aria-label="自动化视图">
          <button type="button" role="tab" aria-selected={section === "tasks"} className={section === "tasks" ? "active" : ""} onClick={() => setSection("tasks")}>全部任务</button>
          <button type="button" role="tab" aria-selected={section === "history"} className={section === "history" ? "active" : ""} onClick={() => setSection("history")}>执行记录</button>
        </div>
        {!embedded && <label>Agent 筛选
          <select aria-label="筛选 Agent" value={agentFilter} onChange={event => setAgentFilter(event.target.value)}>
            <option value="">全部 Agent</option>
            {agents.map(agent => <option key={agent.metadata.id} value={agent.metadata.id}>{agent.metadata.name}</option>)}
          </select>
        </label>}
      </div>
      {section === "tasks" && <div className="automation-layout">
        <section className="automation-list block">
          <StudioDataTable
            columns={columns}
            data={filteredTasks}
            getRowId={task => task.taskId}
            caption="定时任务列表"
            loading={loading}
            error={error && !tasks.length ? error : ""}
            onRetry={() => void loadTasks()}
            onRowActivate={task => { setEditorOpen(false); void loadOccurrences(task); }}
            rowAriaLabel={task => `查看定时任务 ${taskLabel(task)} 的详情`}
            empty={{ icon: <CalendarClock size={22} />, title: agentFilter ? "该 Agent 还没有定时任务" : "还没有定时任务", description: "创建一个本地任务，在 Agent Kernel 保持运行时自动触发。" }}
          />
        </section>
        <aside className={`automation-inspector block${editorOpen ? " is-editor automation-form" : ""}`}>
          {editorOpen ? <>
          <div className="section-heading"><div className="section-heading-copy"><h2>{editingTaskId ? "编辑定时任务" : "新建定时任务"}</h2><p>目标实例和版本由 Studio 解析，不由浏览器填写。</p></div></div>
          <label>Agent<select value={form.agentId} disabled={Boolean(editingTaskId)} onChange={event => { setForm(previous => ({ ...previous, agentId: event.target.value })); onSelectAgent(event.target.value); }}><option value="">选择 Agent</option>{agents.map(agent => <option key={agent.metadata.id} value={agent.metadata.id}>{agent.metadata.name}</option>)}</select></label>
          <label>任务名称<input value={form.displayName} onChange={event => setForm(previous => ({ ...previous, displayName: event.target.value }))} placeholder="例如：工作日销售日报" /></label>
          <label>到期提示词<textarea value={form.prompt} onChange={event => setForm(previous => ({ ...previous, prompt: event.target.value }))} placeholder="例如：生成昨日销售摘要并列出异常" /></label>
          <div className="form-grid two-columns">
            <label>触发方式<select value={form.kind} onChange={event => setForm(previous => ({ ...previous, kind: event.target.value as ScheduleSpec["kind"] }))}><option value="cron">Cron</option><option value="interval">固定间隔</option><option value="once">单次</option></select></label>
            <label>时区<input value={form.timezone} onChange={event => setForm(previous => ({ ...previous, timezone: event.target.value }))} /></label>
          </div>
          {form.kind === "cron" && <label>Cron 表达式<input value={form.expression} onChange={event => setForm(previous => ({ ...previous, expression: event.target.value }))} /></label>}
          {form.kind === "interval" && <label>间隔（秒）<input type="number" min="60" value={form.everySeconds} onChange={event => setForm(previous => ({ ...previous, everySeconds: event.target.value }))} /></label>}
          {form.kind === "once" && <label>执行时间<input type="datetime-local" value={form.at} onChange={event => setForm(previous => ({ ...previous, at: event.target.value }))} /></label>}
          <div className="form-grid two-columns"><label>错过时<select value={form.misfirePolicy} onChange={event => setForm(previous => ({ ...previous, misfirePolicy: event.target.value as ScheduleForm["misfirePolicy"] }))}><option value="run_once">补跑一次</option><option value="skip">跳过</option></select></label><label>会话<select value={form.continuity} onChange={event => setForm(previous => ({ ...previous, continuity: event.target.value as ScheduleForm["continuity"] }))}><option value="new_session">新会话</option><option value="continue_session">继续会话</option></select></label></div>
          {form.continuity === "continue_session" && <label>Session ID<input value={form.sessionId} onChange={event => setForm(previous => ({ ...previous, sessionId: event.target.value }))} placeholder="选择或粘贴可恢复的本地 Session" /></label>}
          <label className="automation-checkbox"><input type="checkbox" checked={form.enabled} onChange={event => setForm(previous => ({ ...previous, enabled: event.target.checked }))} />创建后立即启用</label>
          <div className="automation-form-actions"><button className="button secondary" type="button" onClick={() => { setEditingTaskId(""); setForm(emptyForm(currentAgentId)); setEditorOpen(false); }}>取消</button><button className="button accent" type="button" disabled={submitting} onClick={() => void save()}>{submitting ? "保存中…" : editingTaskId ? "保存变更" : "创建任务"}</button></div>
          </> : selected ? <div className="automation-detail">
            <div className="automation-detail-heading"><div><h2>{taskLabel(selected)}</h2><p>下次执行 {formatTime(selected.nextRunAt)}</p></div><span className="automation-state" data-state={selected.enabled && availability?.triggerActive ? "ready" : "idle"}>{selected.enabled ? "已启用" : "已停用"}</span></div>
            <div className="automation-detail-actions"><button className="button secondary small" type="button" onClick={() => startEdit(selected)}><Pencil size={14} />编辑</button><button className="button secondary small" type="button" disabled={!availability?.available} onClick={() => void runNow(selected)}><Play size={14} />立即运行</button><button className="button secondary small" type="button" onClick={() => void toggle(selected)}>{selected.enabled ? "停用" : "启用"}</button></div>
            <div className="automation-detail-grid"><div><span>到期提示词</span><p>{selected.command.payload.content || "—"}</p></div><div><span>触发规则</span><p>{triggerLabel(selected.schedule)}</p></div><div><span>会话</span><p>{selected.continuity === "continue_session" ? "继续已有会话" : "每次新建会话"}</p></div><div><span>目标 Build</span><p>{selected.target.agentVersionRef || "—"}</p></div><div><span>最近一次</span><p>{occurrences[0] ? `${occurrenceState(occurrences[0].state)} · ${formatTime(occurrences[0].completedAt || occurrences[0].scheduledFor)}` : "暂无记录"}</p></div></div>
            <div className="automation-inspector-history"><h3><Clock3 size={16} />最近执行</h3><OccurrenceHistory items={occurrences.slice(0, 3)} taskNames={taskName} agentNames={agentName} /></div>
            <button className="automation-delete-link" type="button" onClick={() => setDeleteTask(selected)}><Trash2 size={14} />删除</button>
          </div> : <div className="automation-inspector-empty"><CalendarClock size={22}/><strong>选择一个定时任务</strong><p>查看运行记录、立即执行或修改计划。</p><button className="button secondary small" type="button" onClick={() => { setEditingTaskId(""); setForm(emptyForm(currentAgentId)); setEditorOpen(true); }}><Plus size={14}/>新建任务</button></div>}
        </aside>
      </div>}
      {section === "history" && <section className="automation-history block"><div className="section-heading"><div className="section-heading-copy"><h2>执行记录</h2><p>持久化的 Occurrence 事实；“已接收”不代表执行成功。</p></div></div><OccurrenceHistory items={filteredOccurrences} taskNames={taskName} agentNames={agentName} /></section>}
      {deleteTask && <ConfirmDialog title={`删除定时任务「${taskLabel(deleteTask)}」？`} description="任务定义将被删除；已写入的执行历史仍可用于审计。" confirmText="删除任务" busy={false} onConfirm={() => void confirmDelete()} onCancel={() => setDeleteTask(null)} />}
    </div>
  );
}
