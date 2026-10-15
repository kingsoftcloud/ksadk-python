import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("./api", () => ({ apiFetch: mocks.apiFetch }));

import { recordReasoningTokens, reasoningTokensFromEvents, useTurnReasoningTokens } from "./turnReasoningTokens";

function jsonResponse(payload: unknown) {
  return { ok: true, json: async () => payload } as unknown as Response;
}

function sseResponse(blocks: string[]) {
  return { ok: true, text: async () => blocks.join("") } as unknown as Response;
}

const usageBlock = (reasoningTokens: number) =>
  `id: 1\nevent: usage.reported\ndata: ${JSON.stringify({ runId: "run-1", reasoningTokens, inputTokens: 10, outputTokens: 2, totalTokens: 12 })}\n\n`;

describe("turn reasoning tokens", () => {
  beforeEach(() => mocks.apiFetch.mockReset());

  it("sums every usage.reported event of the run", () => {
    expect(reasoningTokensFromEvents([
      { type: "run.started" },
      { type: "usage.reported", reasoningTokens: 25 },
      { type: "thinking.delta", text: "..." },
      { type: "usage.reported", reasoning_tokens: 7 },
    ])).toBe(32);
    expect(reasoningTokensFromEvents([{ type: "run.started" }])).toBeNull();
    expect(reasoningTokensFromEvents([{ type: "usage.reported", reasoningTokens: 0 }])).toBe(0);
  });

  it("falls back to the run record's reported reasoning tokens", () => {
    expect(recordReasoningTokens({ id: "run-2", usage: { reported: true, reasoningOutputTokens: 34 } })).toBe(34);
    expect(recordReasoningTokens({ id: "run-2", usage: { reported: false, reasoningOutputTokens: 34 } })).toBeNull();
    expect(recordReasoningTokens(undefined)).toBeNull();
  });

  it("publishes the settled turn's reasoning tokens to the conversation area", async () => {
    mocks.apiFetch.mockImplementation(async (input: RequestInfo | URL) => (
      String(input).includes("/events")
        ? sseResponse([usageBlock(1000), usageBlock(24)])
        : jsonResponse({ items: [{ id: "run-1", sessionId: "ses-1", status: "COMPLETED" }] })
    ));
    const { result, rerender } = renderHook(
      ({ streaming }: { streaming: boolean }) => useTurnReasoningTokens({ sessionId: "ses-1", streaming }),
      { initialProps: { streaming: false } },
    );
    // Opening a session does not fetch: there is no settled live turn yet.
    expect(mocks.apiFetch).not.toHaveBeenCalled();

    rerender({ streaming: true });
    expect(result.current).toBeNull();
    rerender({ streaming: false });
    await waitFor(() => expect(result.current).toBe(1024));
    expect(String(mocks.apiFetch.mock.calls[0]?.[0])).toContain("/api/v1/runs?sessionId=ses-1");
    expect(String(mocks.apiFetch.mock.calls[1]?.[0])).toContain("/api/v1/runs/run-1/events");
  });

  it("does not poll when disabled", () => {
    const { rerender } = renderHook(
      ({ streaming }: { streaming: boolean }) => useTurnReasoningTokens({ sessionId: "ses-1", streaming, enabled: false }),
      { initialProps: { streaming: true } },
    );
    rerender({ streaming: false });
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });
});
