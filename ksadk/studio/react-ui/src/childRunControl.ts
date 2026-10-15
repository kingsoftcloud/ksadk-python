import type { ControlRun } from "./runControlAdmission";

export type ChildTask = {
  child_run_id: string;
  session_id: string;
  role: string | null;
  phase: string;
  delivery: "foreground" | "background";
  can_cancel?: boolean;
  can_promote: boolean;
  can_send_input: boolean;
  goal_revision: number;
};
export type ChildInput = { command_id: string; text: string; expected_goal_revision: number };
export type ChildControlReceipt = { runId: string; childRunId: string; commandId: string; status: "queued" };
export type ChildRunControls = {
  childTasks: (run: ControlRun, signal?: AbortSignal) => Promise<ChildTask[]>;
  cancelChild?: (run: ControlRun, childRunId: string, commandId: string) => Promise<ChildControlReceipt>;
  promoteChild: (run: ControlRun, childRunId: string, commandId: string) => Promise<ChildControlReceipt>;
  sendChildInput: (run: ControlRun, childRunId: string, input: ChildInput) => Promise<ChildControlReceipt>;
};

export class ChildControlError extends Error {
  constructor(message: string, readonly uncertain = false) { super(message); }
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

/** Host-only REST extension; never decodes or reduces the conversation stream. */
export class ChildRunClient {
  constructor(private readonly fetcher: typeof fetch) {}

  private async request(path: string, input?: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> {
    let response: Response;
    try {
      response = await this.fetcher(path, input ? {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
      } : { signal });
    } catch (error) {
      if (!input) throw error;
      throw new ChildControlError("投递状态未知，请用同一条补充重试确认。", true);
    }
    if (!response.ok) {
      // A lost/failed response can follow an accepted command. Explicit retry
      // therefore retains its original ID and payload; it never starts a Run.
      const uncertain = Boolean(input) && (response.status >= 500 || response.status === 408);
      throw new ChildControlError(uncertain ? "投递状态未知，请用原请求重试确认。"
        : "子任务操作未被接收，请刷新状态后重试。", uncertain);
    }
    try { return await response.json(); }
    catch { throw new ChildControlError("运行时返回了无法确认的子任务回执。", Boolean(input)); }
  }

  async childTasks(run: ControlRun, signal?: AbortSignal): Promise<ChildTask[]> {
    const data = record(await this.request(`/api/v1/runs/${encodeURIComponent(run.runId)}/children`, undefined, signal));
    if (data?.runId !== run.runId || !Array.isArray(data.tasks)) {
      throw new ChildControlError("子任务目录与当前运行不一致。请刷新状态。");
    }
    const seen = new Set<string>();
    return data.tasks.map(value => {
      const row = record(value);
      if (!row || typeof row.child_run_id !== "string" || !row.child_run_id
        || seen.has(row.child_run_id) || typeof row.session_id !== "string" || !row.session_id
        || !(row.role === null || typeof row.role === "string")
        || typeof row.phase !== "string" || !row.phase
        || !["foreground", "background"].includes(String(row.delivery))
        || (row.can_cancel !== undefined && typeof row.can_cancel !== "boolean")
        || typeof row.can_promote !== "boolean" || typeof row.can_send_input !== "boolean"
        || !Number.isSafeInteger(row.goal_revision) || Number(row.goal_revision) < 0) {
        throw new ChildControlError("子任务目录不完整。请刷新状态。");
      }
      seen.add(row.child_run_id);
      return {
        child_run_id: row.child_run_id, session_id: row.session_id, role: row.role,
        phase: row.phase, delivery: row.delivery as ChildTask["delivery"],
        ...(row.can_cancel === undefined ? {} : { can_cancel: row.can_cancel }),
        can_promote: row.can_promote, can_send_input: row.can_send_input,
        goal_revision: Number(row.goal_revision),
      } as ChildTask;
    });
  }

  private async command(run: ControlRun, childRunId: string, action: string, input: Record<string, unknown>) {
    const data = record(await this.request(`/api/v1/runs/${encodeURIComponent(run.runId)}/children/${encodeURIComponent(childRunId)}:${action}`, input));
    if (data?.runId !== run.runId || data.childRunId !== childRunId
      || data.commandId !== input.command_id || data.status !== "queued") {
      throw new ChildControlError("运行时尚未确认原子任务请求，请用原请求重试。", true);
    }
    return data as ChildControlReceipt;
  }

  cancelChild(run: ControlRun, childRunId: string, commandId: string) {
    return this.command(run, childRunId, "cancel", { command_id: commandId });
  }
  promoteChild(run: ControlRun, childRunId: string, commandId: string) {
    return this.command(run, childRunId, "promote", { command_id: commandId });
  }
  sendChildInput(run: ControlRun, childRunId: string, input: ChildInput) {
    return this.command(run, childRunId, "send", {
      command_id: input.command_id, text: input.text, expected_goal_revision: input.expected_goal_revision,
    });
  }
}
