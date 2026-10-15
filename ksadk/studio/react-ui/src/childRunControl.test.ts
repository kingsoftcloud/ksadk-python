import { describe, expect, it, vi } from "vitest";
import { ChildControlError, ChildRunClient } from "./childRunControl";
import { StudioChatApiFacade } from "./studioChatApiFacade";

const run = { agentId: "agent", sessionId: "session", invocationId: "inv", runId: "host/run" };
const child = { child_run_id: "child/a", session_id: "child-session", role: "reviewer",
  phase: "requesting_model", delivery: "foreground", can_promote: true, can_send_input: true, goal_revision: 2 };
const input = { command_id: "input-1", text: "Check the actual file.", expected_goal_revision: 2 };
const receipt = { runId: run.runId, childRunId: child.child_run_id, commandId: input.command_id, status: "queued" };
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });

describe("original child controls", () => {
  it("uses the actual host Run, passes GET abort only, and reads original directory facts", async () => {
    const fetcher = vi.fn(async () => response({ runId: run.runId, tasks: [child] }));
    const signal = new AbortController().signal;
    expect(await new ChildRunClient(fetcher).childTasks(run, signal)).toEqual([child]);
    expect(fetcher).toHaveBeenCalledWith("/api/v1/runs/host%2Frun/children", { signal });
  });
  it.each([
    { runId: "other", tasks: [child] },
    { runId: run.runId, tasks: [child, child] },
    { runId: run.runId, tasks: [{ ...child, goal_revision: true }] },
    { runId: run.runId, tasks: [{ ...child, can_send_input: "true" }] },
    { runId: run.runId, tasks: [{ ...child, can_cancel: "true" }] },
  ])("rejects unbound/incomplete directory data", async data => {
    await expect(new ChildRunClient(async () => response(data)).childTasks(run)).rejects.toBeInstanceOf(ChildControlError);
  });
  it("posts only original input identity/text/revision and confirms only queued", async () => {
    const fetcher = vi.fn(async () => response(receipt, 202));
    expect(await new ChildRunClient(fetcher).sendChildInput(run, child.child_run_id, input)).toEqual(receipt);
    const [path, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/v1/runs/host%2Frun/children/child%2Fa:send");
    expect(JSON.parse(String(options.body))).toEqual(input);
    expect(options).not.toHaveProperty("signal");
  });
  it.each(["runId", "childRunId", "commandId", "status"])("retains unknown delivery for a mismatched %s receipt", async key => {
    const client = new ChildRunClient(async () => response({ ...receipt, [key]: "other" }, 202));
    await expect(client.sendChildInput(run, child.child_run_id, input)).rejects.toMatchObject({ uncertain: true });
  });
  it("distinguishes network/5xx uncertainty from a rejected request", async () => {
    await expect(new ChildRunClient(async () => { throw new TypeError("offline"); }).sendChildInput(run, child.child_run_id, input))
      .rejects.toMatchObject({ uncertain: true });
    await expect(new ChildRunClient(async () => response({}, 503)).sendChildInput(run, child.child_run_id, input))
      .rejects.toMatchObject({ uncertain: true });
    await expect(new ChildRunClient(async () => response({}, 409)).sendChildInput(run, child.child_run_id, input))
      .rejects.toMatchObject({ uncertain: false });
  });
  it("requires a scoped host receipt before a facade can address any child", async () => {
    const fetcher = vi.fn(async () => response({}));
    const facade = new StudioChatApiFacade({ fetch: fetcher, agentId: "agent" });
    await expect(facade.sendChildInput(run, child.child_run_id, input)).rejects.toThrow("会话已切换");
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("keeps the host Run binding through the untouched shared stream", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const source = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    const fetcher = vi.fn(async (path: RequestInfo | URL) => String(path).includes("RunAgent")
      ? new Response(source) : response(receipt, 202));
    const facade = new StudioChatApiFacade({ fetch: fetcher, agentId: "agent" });
    const stream = await facade.runAgent({ AgentId: "agent", SessionId: "session", InvocationId: "inv" });
    const reader = stream.getReader();
    const read = reader.read();
    controller.enqueue(new TextEncoder().encode(`event: studio.run.control_ready\ndata: ${JSON.stringify({ AgentId: "agent", SessionId: "session", InvocationId: "inv", RunId: run.runId,
      ChildControls: { input: true, promotion: false, cancellation: true } })}\n\n`));
    await read;
    expect(facade.controlRun("session")).toEqual({ ...run, childControls: { input: true, promotion: false, cancellation: true } });
    await expect(facade.promoteChild(run, child.child_run_id, "promotion")).rejects.toThrow("未声明");
    await expect(facade.sendChildInput({ ...run, runId: "forged" }, child.child_run_id, input)).rejects.toThrow("会话已切换");
    await expect(facade.sendChildInput(run, child.child_run_id, input)).resolves.toEqual(receipt);
    await expect(facade.cancelChild(run, child.child_run_id, input.command_id)).resolves.toEqual(receipt);
    const cancelRequest = fetcher.mock.calls.at(-1)!;
    expect(String(cancelRequest[0])).toContain("/children/child%2Fa:cancel");
    const end = reader.read(); controller.close(); await end;
    await expect(facade.childTasks(run)).rejects.toThrow("运行已结束");
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});
