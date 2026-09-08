import { describe, expect, it } from "vitest";
import { emptyForm, formFromTask, payloadFromForm, triggerLabel, zonedDateTimeToISO, type ScheduledTask } from "./automationModel";

describe("automation calendar", () => {
  it("uses intervals for non-divisors of one hour and daily time for calendar schedules", () => {
    expect(payloadFromForm(emptyForm()).schedule.expression).toBe("0 10 * * *");
    const form = { ...emptyForm(), kind: "interval" as const, intervalValue: "7" };
    expect(payloadFromForm(form).schedule).toEqual({ kind: "interval", timezone: "Asia/Shanghai", misfirePolicy: "skip", everySeconds: 420 });
    expect(triggerLabel(payloadFromForm(form).schedule)).toBe("每 7 分钟");
  });
  it("roundtrips a one-time task using its timezone, independently of the browser zone", () => {
    const task = { taskId: "task-1", target: { agentId: "agent-1" }, schedule: { kind: "once", timezone: "Asia/Shanghai", at: "2030-01-10T02:00:00Z" }, command: { payload: { content: "日报" } }, enabled: true, continuity: "new_session" } as ScheduledTask;
    const form = formFromTask(task);
    expect(form.at).toBe("2030-01-10T10:00");
    expect(payloadFromForm(form).schedule.at).toBe("2030-01-10T02:00:00.000Z");
  });
  it("rejects missing and ambiguous daylight saving times", () => {
    expect(() => zonedDateTimeToISO("2030-03-10T02:30", "America/New_York")).toThrow("夏令时");
    expect(() => zonedDateTimeToISO("2030-11-03T01:30", "America/New_York")).toThrow("重复");
  });
  it("keeps custom cron and exact existing intervals when editing", () => {
    const task = { taskId: "task-1", target: { agentId: "agent-1" }, schedule: { kind: "cron", timezone: "UTC", expression: "12 7 1 * *" }, command: { payload: { content: "月报" } }, enabled: true, continuity: "new_session" } as ScheduledTask;
    expect(formFromTask(task).preset).toBe("custom");
    expect(payloadFromForm(formFromTask(task)).schedule.expression).toBe("12 7 1 * *");
    task.schedule = { kind: "interval", timezone: "UTC", everySeconds: 91 };
    expect(payloadFromForm(formFromTask(task)).schedule.everySeconds).toBe(91);
  });
});
