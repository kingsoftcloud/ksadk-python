import { OutboxStore, type ConversationController, type ConversationId } from "@kingsoftcloud/ksadk-web/conversation";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { parseRunReceipt, reconcileOutboxEntry, type RunReceipt } from "./runReceiptRecovery";
import { useRunReceiptRecovery } from "./useRunReceiptRecovery";

const conversationId: ConversationId = "conversation_original";
const identity = { agentId: "agent", sessionId: "session", invocationId: "client-original" };
function receipt(Status: RunReceipt["Status"] = "completed"): RunReceipt {
  return { SchemaVersion: 1, AgentId: "agent", SessionId: "session", InvocationId: "client-original",
    RunId: "different-real-host-run", Status, Source: "run" };
}
function fixture() {
  const outbox = new OutboxStore();
  const queued = outbox.enqueue({ agentId: "agent", conversationId, text: "original", attachments: [],
    nativeSessionId: "session", invocationId: "client-original" });
  const entry = outbox.update(queued.requestId, { status: "unknown" })!;
  const api = { getRunReceipt: vi.fn(async () => receipt()) };
  const options = { api, outbox, entry, agentId: "agent", conversationId,
    signal: new AbortController().signal, isRequestActive: vi.fn(() => false) };
  return { ...options, options };
}

describe("original submission receipts", () => {
  it.each([
    ["completed", "completed"], ["running", "sending"], ["created", "sending"],
    ["paused", "sending"], ["waiting_input", "sending"], ["failed", "failed"],
    ["cancelled", "cancelled"], ["interrupted", "failed"], ["timed_out", "failed"],
  ] as const)("consumes %s only for the original submission as %s", async (state, status) => {
    const f = fixture();
    f.api.getRunReceipt.mockResolvedValue(receipt(state));
    await reconcileOutboxEntry(f.options);
    expect(f.outbox.get(f.entry.requestId)?.status).toBe(status);
    expect(f.outbox.get(f.entry.requestId)?.attempt).toBe(0);
    expect(f.api.getRunReceipt).toHaveBeenCalledExactlyOnceWith("agent", "session", "client-original", f.signal);
  });
  it("retains unknown instead of inferring a retry or delivery", async () => {
    const f = fixture();
    f.api.getRunReceipt.mockResolvedValue({ ...receipt("unknown"), Source: "unavailable", RunId: "" });
    await reconcileOutboxEntry(f.options);
    expect(f.outbox.get(f.entry.requestId)?.status).toBe("unknown");
    expect(f.outbox.list()).toHaveLength(1);
  });
  it("operation admission proves accepted input, not a completed Run", async () => {
    const f = fixture();
    f.api.getRunReceipt.mockResolvedValue({ ...receipt("queued"), Source: "operation", RunId: "" });
    await reconcileOutboxEntry(f.options);
    expect(f.outbox.get(f.entry.requestId)?.status).toBe("sending");
    expect(() => parseRunReceipt({ ...receipt(), Source: "operation", RunId: "" }, identity)).toThrow();
  });
  it.each(["AgentId", "SessionId", "InvocationId", "SchemaVersion", "Status", "Source", "RunId"])("rejects a mismatched %s", async field => {
    const f = fixture();
    const bad = { ...receipt(), [field]: field === "RunId" ? "" : "different" };
    f.api.getRunReceipt.mockResolvedValue(bad as unknown as RunReceipt);
    await reconcileOutboxEntry(f.options);
    expect(f.outbox.get(f.entry.requestId)?.status).toBe("unknown");
  });
  it("network failure keeps the original input and invocation", async () => {
    const f = fixture();
    f.api.getRunReceipt.mockRejectedValue(new Error("fixture-network"));
    await reconcileOutboxEntry(f.options);
    expect(f.outbox.get(f.entry.requestId)).toMatchObject({ status: "unknown", text: "original", invocationId: "client-original", attempt: 0 });
  });
  it("late receipt cannot overwrite a concurrently settled entry", async () => {
    const f = fixture();
    let resolve!: (value: RunReceipt) => void;
    f.api.getRunReceipt.mockImplementation(() => new Promise(r => { resolve = r; }));
    const work = reconcileOutboxEntry(f.options);
    f.outbox.update(f.entry.requestId, { status: "cancelled" });
    resolve(receipt());
    await work;
    expect(f.outbox.get(f.entry.requestId)?.status).toBe("cancelled");
  });
  it("scope detachment discards a late reply even if fetch ignores abort", async () => {
    const f = fixture();
    let resolve!: (value: RunReceipt) => void;
    f.api.getRunReceipt.mockImplementation(() => new Promise(r => { resolve = r; }));
    const controller = new AbortController();
    const work = reconcileOutboxEntry({ ...f.options, signal: controller.signal });
    controller.abort();
    resolve(receipt());
    await work;
    expect(f.outbox.get(f.entry.requestId)).toBe(f.entry);
  });
  it("uses only the original scoped binding when entry lacks a session", async () => {
    const f = fixture();
    const entry = f.outbox.update(f.entry.requestId, { nativeSessionId: undefined })!;
    await reconcileOutboxEntry({ ...f.options, entry, binding: { agentId: "other", conversationId, nativeSessionId: "newest" } });
    expect(f.api.getRunReceipt).not.toHaveBeenCalled();
    await reconcileOutboxEntry({ ...f.options, entry, binding: { agentId: "agent", conversationId, nativeSessionId: "session" } });
    expect(f.outbox.get(entry.requestId)?.status).toBe("completed");
  });
  it("does not take over an original request still owned by the shared queue", async () => {
    const f = fixture();
    await reconcileOutboxEntry({ ...f.options, isRequestActive: () => true });
    expect(f.api.getRunReceipt).not.toHaveBeenCalled();
  });
});

describe("restore and transport recovery", () => {
  const controller = { binding: () => undefined } as unknown as ConversationController;
  it("queries once at restore and at transport settlement, not per outbox notification", async () => {
    const f = fixture();
    f.api.getRunReceipt.mockResolvedValue(receipt("running"));
    const { rerender } = renderHook(props => useRunReceiptRecovery({ ...f, controller,
      enabled: true, streaming: props.streaming, revision: props.revision }), {
      initialProps: { streaming: true, revision: 0 },
    });
    await waitFor(() => expect(f.outbox.get(f.entry.requestId)?.status).toBe("sending"));
    rerender({ streaming: true, revision: 1 });
    await act(async () => {});
    expect(f.api.getRunReceipt).toHaveBeenCalledTimes(1);
    f.api.getRunReceipt.mockResolvedValue(receipt());
    rerender({ streaming: false, revision: 2 });
    await waitFor(() => expect(f.outbox.get(f.entry.requestId)?.status).toBe("completed"));
    expect(f.api.getRunReceipt).toHaveBeenCalledTimes(2);
  });
  it("disabled targets do not query unsupported or cloud receipts", async () => {
    const f = fixture();
    const { result } = renderHook(() => useRunReceiptRecovery({ ...f, controller,
      enabled: false, streaming: false, revision: 0 }));
    await act(() => result.current(f.entry.requestId));
    expect(f.api.getRunReceipt).not.toHaveBeenCalled();
  });
  it("manual confirmation joins the ongoing original query", async () => {
    const f = fixture();
    let resolve!: (value: RunReceipt) => void;
    f.api.getRunReceipt.mockImplementation(() => new Promise(r => { resolve = r; }));
    const { result } = renderHook(() => useRunReceiptRecovery({ ...f, controller,
      enabled: true, streaming: false, revision: 0 }));
    await waitFor(() => expect(f.api.getRunReceipt).toHaveBeenCalledTimes(1));
    const manual = result.current(f.entry.requestId);
    await act(async () => { resolve(receipt()); await manual; });
    expect(f.api.getRunReceipt).toHaveBeenCalledTimes(1);
  });
});
