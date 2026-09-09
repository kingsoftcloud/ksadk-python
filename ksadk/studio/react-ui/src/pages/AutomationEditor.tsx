import { useEffect, useState } from "react";
import { CalendarClock } from "lucide-react";
import { apiFetch } from "../api";
import { Drawer } from "../components/Drawer";
import { type AgentSummary, type ScheduleForm, payloadFromForm, triggerLabel } from "./automationModel";

export function AutomationEditor({ form, agents, editing, busy, error, scoped, onChange, onSave, onClose }: {
  form: ScheduleForm; agents: AgentSummary[]; editing: boolean; busy: boolean; error: string; scoped: boolean;
  onChange: (form: ScheduleForm) => void; onSave: () => void; onClose: () => void;
}) {
  const [sessions, setSessions] = useState<Array<{ SessionId: string; Title: string }>>([]);
  const [sessionError, setSessionError] = useState("");
  const [loadingSessions, setLoadingSessions] = useState(false);
  const set = <K extends keyof ScheduleForm>(key: K, value: ScheduleForm[K]) => onChange({ ...form, [key]: value });
  useEffect(() => {
    let active = true;
    setSessions([]);
    setSessionError("");
    if (form.continuity !== "continue_session" || !form.agentId) return;
    setLoadingSessions(true);
    void (async () => {
      try {
        const items: Array<{ SessionId: string; Title: string }> = [];
        for (let page = 1; ; page++) {
          const response = await apiFetch("/agentengine/api/v1/ListSessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ AgentId: form.agentId, Page: page, PageSize: 100 }) });
          if (!response.ok) throw new Error("会话加载失败，请重新选择对话方式后重试。");
          const payload = await response.json();
          if (payload.Code) throw new Error(payload.Message || "会话加载失败");
          const batch = payload.Data?.Sessions || [];
          items.push(...batch);
          if (!active) return;
          if (!batch.length || items.length >= (payload.Data?.Total || batch.length)) break;
        }
        if (active) setSessions(items);
      } catch (error) { if (active) setSessionError((error as Error).message); }
      finally { if (active) setLoadingSessions(false); }
    })();
    return () => { active = false; };
  }, [form.agentId, form.continuity]);

  let summary = "设置任务的执行时间";
  try { summary = triggerLabel(payloadFromForm({ ...form, continuity: "new_session" }).schedule); } catch { /* Incomplete drafts have no preview. */ }

  return <Drawer title={editing ? "编辑定时任务" : "新建定时任务"} subtitle="安排好下一次，把重复的工作交给智能体。" closeDisabled={busy} onClose={onClose}
    footer={<><button className="button secondary" disabled={busy} onClick={onClose}>取消</button><button className="button accent" disabled={busy || !form.agentId || !form.prompt.trim()} onClick={onSave}>{busy ? "保存中…" : editing ? "保存变更" : "创建任务"}</button></>}>
    <div className="automation-editor">
      {error && <div className="form-error" role="alert">{error}</div>}
      <label>交给谁<select value={form.agentId} disabled={editing || scoped} onChange={event => onChange({ ...form, agentId: event.target.value, sessionId: "" })}><option value="">选择智能体</option>{agents.map(agent => <option key={agent.metadata.id} value={agent.metadata.id}>{agent.metadata.name}</option>)}</select></label>
      <label>任务名称<input maxLength={128} value={form.displayName} onChange={event => set("displayName", event.target.value)} placeholder="例如：每日工作简报" /></label>
      <label>任务说明<textarea aria-label="任务说明" maxLength={32768} rows={4} value={form.prompt} onChange={event => set("prompt", event.target.value)} placeholder="到时间后需要做什么？例如：整理昨日进展，列出待办和风险。" /></label>
      <fieldset><legend>何时执行</legend>
        <div className="automation-frequency" role="group" aria-label="计划类型">{([["cron", "固定时间"], ["interval", "固定间隔"], ["once", "仅一次"]] as const).map(([kind, label]) => <button key={kind} type="button" aria-pressed={form.kind === kind} onClick={() => onChange({ ...form, kind, misfirePolicy: kind === "once" ? "run_once" : "skip" })}>{label}</button>)}</div>
        {form.kind === "cron" && <>
          <div className="form-grid two-columns"><label>重复<select value={form.preset} onChange={event => set("preset", event.target.value as ScheduleForm["preset"])}><option value="daily">每天</option><option value="weekdays">工作日（周一至周五）</option><option value="weekly">每周</option><option value="custom">自定义 Cron</option></select></label>
          {form.preset !== "custom" && <label>执行时间<input type="time" value={form.time} onChange={event => set("time", event.target.value)} /></label>}</div>
          {form.preset === "weekly" && <label>星期<select value={form.weekday} onChange={event => set("weekday", event.target.value)}>{[1, 2, 3, 4, 5, 6, 0].map(day => <option value={day} key={day}>星期{"日一二三四五六"[day]}</option>)}</select></label>}
          {form.preset === "custom" && <label>Cron 表达式<input value={form.expression} onChange={event => set("expression", event.target.value)} placeholder="0 10 * * *" /><small>依次为分钟、小时、日期、月份、星期。</small></label>}
        </>}
        {form.kind === "interval" && <div className="form-grid two-columns"><label>每隔<input type="number" min={1} value={form.intervalValue} onChange={event => set("intervalValue", event.target.value)} /></label><label>时间单位<select value={form.intervalUnit} onChange={event => set("intervalUnit", event.target.value as ScheduleForm["intervalUnit"])}><option value="60">分钟</option><option value="3600">小时</option><option value="86400">天</option>{form.intervalUnit === "1" && <option value="1">秒</option>}</select></label></div>}
        {form.kind === "once" && <label>执行日期与时间<input type="datetime-local" value={form.at} onChange={event => set("at", event.target.value)} /></label>}
        <label>时区<input list="automation-timezones" value={form.timezone} onChange={event => set("timezone", event.target.value)} /><datalist id="automation-timezones">{["Asia/Shanghai", "Asia/Tokyo", "UTC", "America/New_York", "Europe/London"].map(zone => <option key={zone} value={zone} />)}</datalist></label>
        <div className="automation-schedule-preview"><CalendarClock size={18} /><span>{summary}<small>{form.timezone}</small></span></div>
      </fieldset>
      <label>对话方式<select value={form.continuity} onChange={event => set("continuity", event.target.value as ScheduleForm["continuity"])}><option value="new_session">每次新建会话</option><option value="continue_session">继续已有会话</option></select><small>日报、巡检建议每次新建会话，结果更容易查找。</small></label>
      {form.continuity === "continue_session" && <label>选择会话<select value={form.sessionId} onChange={event => set("sessionId", event.target.value)} disabled={loadingSessions}><option value="">{loadingSessions ? "正在加载…" : "选择该智能体的会话"}</option>{form.sessionId && !sessions.some(item => item.SessionId === form.sessionId) && <option value={form.sessionId}>当前绑定的会话 · {form.sessionId}</option>}{sessions.map(item => <option key={item.SessionId} value={item.SessionId}>{item.Title || "未命名会话"} · {item.SessionId.slice(-8)}</option>)}</select>{sessionError && <small role="alert">{sessionError}</small>}</label>}
      <details className="automation-advanced"><summary>更多设置</summary><label>错过执行时间时<select value={form.misfirePolicy} onChange={event => set("misfirePolicy", event.target.value as ScheduleForm["misfirePolicy"])}><option value="skip">跳过，等待下一次</option><option value="run_once">恢复后补跑一次</option></select></label><label className="automation-checkbox"><input type="checkbox" checked={form.enabled} onChange={event => set("enabled", event.target.checked)} />启用此任务</label></details>
      <p className="automation-local-note">任务在此设备执行。请保持 Studio 运行；同一任务尚未完成时，会跳过重叠的触发。</p>
    </div>
  </Drawer>;
}
