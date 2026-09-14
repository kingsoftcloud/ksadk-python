export interface AgentSummary {
  metadata: { id: string; name: string };
}

export interface ScheduleSpec {
  kind: "once" | "interval" | "cron";
  timezone: string;
  at?: string | null;
  everySeconds?: number | null;
  expression?: string | null;
  misfirePolicy?: "skip" | "run_once";
}

export interface ScheduledTask {
  taskId: string;
  displayName?: string | null;
  target: { agentId?: string | null; agentVersionRef?: string | null; sessionId?: string | null };
  schedule: ScheduleSpec;
  command: { payload: { content?: string } };
  enabled: boolean;
  continuity: "new_session" | "continue_session";
  nextRunAt?: string | null;
}

export interface ScheduleOccurrence {
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

export interface SchedulerAvailability {
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

export interface ScheduleForm {
  agentId: string;
  displayName: string;
  prompt: string;
  kind: ScheduleSpec["kind"];
  timezone: string;
  at: string;
  expression: string;
  misfirePolicy: "skip" | "run_once";
  enabled: boolean;
  continuity: "new_session" | "continue_session";
  sessionId: string;
  preset: "daily" | "weekdays" | "weekly" | "custom";
  time: string;
  weekday: string;
  intervalValue: string;
  intervalUnit: "60" | "3600" | "86400" | "1";
}

export function formatTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

export function taskLabel(task: ScheduledTask) {
  return task.displayName || task.command.payload.content?.slice(0, 28) || task.taskId;
}

export function triggerLabel(schedule: ScheduleSpec) {
  if (schedule.kind === "once") return `单次 · ${schedule.at ? zonedDateTime(schedule.at, schedule.timezone).replace("T", " ") : "待设置"}`;
  if (schedule.kind === "interval") {
    const seconds = schedule.everySeconds || 60;
    for (const [unit, label] of [[86400, "天"], [3600, "小时"], [60, "分钟"]] as const) {
      if (seconds % unit === 0) return `每 ${seconds / unit} ${label}`;
    }
    return `每 ${seconds} 秒`;
  }
  const match = /^(\d+) (\d+) \* \* (\*|1-5|[0-7])$/.exec(schedule.expression || "");
  if (!match) return schedule.expression || "自定义时间";
  const [, minute, hour, day] = match;
  const label = day === "*" ? "每天" : day === "1-5" ? "工作日" : `每周${"日一二三四五六日"[Number(day)]}`;
  return `${label} ${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
}

export function emptyForm(agentId = ""): ScheduleForm {
  return {
    agentId,
    displayName: "",
    prompt: "",
    kind: "cron",
    timezone: "Asia/Shanghai",
    at: "",
    expression: "0 10 * * *",
    misfirePolicy: "skip",
    enabled: true,
    continuity: "new_session",
    sessionId: "",
    preset: "daily", time: "10:00", weekday: "1", intervalValue: "30", intervalUnit: "60",
  };
}

export function formFromTask(task: ScheduledTask): ScheduleForm {
  const match = /^(\d+) (\d+) \* \* (\*|1-5|[0-7])$/.exec(task.schedule.expression || "");
  const seconds = task.schedule.everySeconds || 3600;
  const divisor = [86400, 3600, 60, 1].find(unit => seconds % unit === 0)!;
  return {
    ...emptyForm(task.target.agentId || ""),
    agentId: task.target.agentId || "",
    displayName: task.displayName || "",
    prompt: task.command.payload.content || "",
    kind: task.schedule.kind,
    timezone: task.schedule.timezone || "Asia/Shanghai",
    at: task.schedule.at ? zonedDateTime(task.schedule.at, task.schedule.timezone) : "",
    expression: task.schedule.expression || "0 9 * * 1-5",
    misfirePolicy: task.schedule.misfirePolicy || "skip",
    enabled: task.enabled,
    continuity: task.continuity,
    sessionId: task.target.sessionId || "",
    preset: !match ? "custom" : match[3] === "*" ? "daily" : match[3] === "1-5" ? "weekdays" : "weekly",
    time: match ? `${match[2].padStart(2, "0")}:${match[1].padStart(2, "0")}` : "10:00",
    weekday: match && /^[0-7]$/.test(match[3]) ? String(Number(match[3]) % 7) : "1",
    intervalValue: String(seconds / divisor), intervalUnit: String(divisor) as ScheduleForm["intervalUnit"],
  };
}

export function payloadFromForm(form: ScheduleForm) {
  const schedule: ScheduleSpec = {
    kind: form.kind,
    timezone: form.timezone.trim() || "Asia/Shanghai",
    misfirePolicy: form.misfirePolicy,
  };
  if (form.kind === "once") {
    if (!form.at) throw new Error("请填写单次任务的执行时间");
    schedule.at = zonedDateTimeToISO(form.at, schedule.timezone);
  } else if (form.kind === "interval") {
    const seconds = Number(form.intervalValue) * Number(form.intervalUnit);
    if (!Number.isInteger(seconds) || seconds < 60) throw new Error("间隔至少为 60 秒");
    schedule.everySeconds = seconds;
  } else {
    if (form.preset === "custom") {
      if (!form.expression.trim()) throw new Error("请填写 Cron 表达式");
      schedule.expression = form.expression.trim();
    } else {
      if (!/^\d{2}:\d{2}$/.test(form.time)) throw new Error("请选择执行时间");
      const [hour, minute] = form.time.split(":").map(Number);
      schedule.expression = `${minute} ${hour} * * ${form.preset === "daily" ? "*" : form.preset === "weekdays" ? "1-5" : form.weekday}`;
    }
  }
  if (form.continuity === "continue_session" && !form.sessionId) throw new Error("请选择要继续的会话");
  return {
    displayName: form.displayName.trim() || form.prompt.trim().slice(0, 32),
    prompt: form.prompt.trim(),
    schedule,
    enabled: form.enabled,
    continuity: form.continuity,
    sessionId: form.continuity === "continue_session" ? form.sessionId.trim() || null : null,
  };
}

export function zonedDateTime(value: string, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(value));
  const get = (type: string) => parts.find(part => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

export function zonedDateTimeToISO(value: string, timeZone: string): string {
  const wall = Date.parse(`${value}:00Z`);
  if (!Number.isFinite(wall)) throw new Error("请填写有效的执行时间");
  let utc = wall;
  for (let i = 0; i < 4; i++) {
    const rendered = Date.parse(`${zonedDateTime(new Date(utc).toISOString(), timeZone)}:00Z`);
    utc += wall - rendered;
  }
  if (zonedDateTime(new Date(utc).toISOString(), timeZone) !== value) throw new Error("该时间处于夏令时跳转，请选择其他时间");
  if ([-3600000, 3600000].some(offset => zonedDateTime(new Date(utc + offset).toISOString(), timeZone) === value)) throw new Error("该时间在夏令时切换时重复，请选择其他时间");
  return new Date(utc).toISOString();
}
