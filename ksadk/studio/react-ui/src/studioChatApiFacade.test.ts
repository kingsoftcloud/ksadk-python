import { describe, expect, it, vi } from "vitest";
import { StudioChatApiFacade } from "./studioChatApiFacade";

describe("StudioChatApiFacade", () => {
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

    facade.authorizeUserCancellation();
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
