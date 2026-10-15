import { describe, expect, it, vi } from "vitest";
import { StudioChatApiFacade } from "./studioChatApiFacade";

describe("StudioChatApiFacade", () => {
  it("queries the original scoped submission with no RunAgent retry", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ Code: 0, Message: "OK", Data: {
      SchemaVersion: 1, AgentId: "agent", SessionId: "session", InvocationId: "client",
      RunId: "actual-host-run", Status: "completed", Source: "run",
    } }), { status: 200, headers: { "content-type": "application/json" } }));
    const facade = new StudioChatApiFacade({ fetch: fetcher, agentId: "agent" });
    const signal = new AbortController().signal;
    await expect(facade.getRunReceipt("agent", "session", "client", signal)).resolves.toMatchObject({ RunId: "actual-host-run" });
    const [url, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("GetRunReceipt");
    expect(options.signal).toBe(signal);
    expect(JSON.parse(String(options.body))).toEqual({ AgentId: "agent", SessionId: "session", InvocationId: "client" });
    await expect(facade.getRunReceipt("other", "session", "client")).rejects.toThrow("原消息");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("uses the published scoped RPC client for SteerRun", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ Code: 0, Message: "OK", Data: {
      InvocationId: "inv-1", RunId: "run-1", Status: "steering", MessageId: "run-1:steer:42",
    } }), { status: 200, headers: { "content-type": "application/json" } }));
    const facade = new StudioChatApiFacade({ fetch: fetcher, agentId: "agent-1" });
    await expect(facade.steerRun("agent-1", "inv-1", "检查测试")).resolves.toMatchObject({ Status: "steering" });
    const [url, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("SteerRun");
    expect(JSON.parse(String(options.body))).toMatchObject({ AgentId: "agent-1", InvocationId: "inv-1", Text: "检查测试" });
  });

  it("rejects a steering receipt for a different invocation", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ Code: 0, Message: "OK", Data: {
      InvocationId: "other", RunId: "run-1", Status: "steering",
    } }), { status: 200, headers: { "content-type": "application/json" } }));
    await expect(new StudioChatApiFacade({ fetch: fetcher }).steerRun("agent", "inv-1", "纠偏")).rejects.toThrow("未确认本轮纠偏");
  });
  it("detaches an internal UI abort without cancelling the backend Run request", async () => {
    let sourceCancelled = false;
    const source = new ReadableStream<Uint8Array>({
      cancel() { sourceCancelled = true; },
    });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.signal).toBeUndefined();
      return new Response(source, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });
    const facade = new StudioChatApiFacade({ fetch: fetcher });
    const lifecycle = new AbortController();
    const stream = await facade.runAgent(
      { AgentId: "agent", SessionId: "session", InvocationId: "run" },
      { signal: lifecycle.signal },
    );
    const read = stream.getReader().read();

    lifecycle.abort();

    await expect(read).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(sourceCancelled).toBe(true));
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("forwards CancelRun only after an explicit user action", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({
        Code: 0,
        Message: "OK",
        Data: { Cancelled: true },
      }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    const facade = new StudioChatApiFacade({ fetch: fetcher });

    await expect(facade.cancelRun("agent", "session", "run")).resolves.toMatchObject({
      Cancelled: false,
      Ignored: true,
    });
    expect(fetcher).not.toHaveBeenCalled();

    facade.authorizeUserCancellation("agent", "session", "run");
    await expect(facade.cancelRun("agent", "other-session", "run")).resolves.toMatchObject({ Ignored: true });
    await expect(facade.cancelRun("agent", "session", "other-run")).resolves.toMatchObject({ Ignored: true });
    expect(fetcher).not.toHaveBeenCalled();
    await facade.cancelRun("agent", "session", "run");
    expect(fetcher).toHaveBeenCalledOnce();
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toMatchObject({
      AgentId: "agent",
      SessionId: "session",
      InvocationId: "run",
    });

    await expect(facade.cancelRun("agent", "session", "run")).resolves.toMatchObject({
      Cancelled: false,
      Ignored: true,
    });
    expect(fetcher).toHaveBeenCalledOnce();
  });
});
