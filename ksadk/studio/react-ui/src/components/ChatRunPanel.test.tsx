import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ChatRunPanel, resolveMemoryRecallPresentation } from "./ChatRunPanel";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

describe("resolveMemoryRecallPresentation", () => {
  it("does not claim recalled memory was used until projection is confirmed", () => {
    expect(resolveMemoryRecallPresentation([
      {
        id: 1,
        type: "memory.recall.completed",
        data: { candidate_count: 2, provider: "local-default" },
      },
    ], 0)).toEqual({
      status: "recalled",
      title: "已召回长期记忆",
      description: "已找到 2 条，但未确认交付 Runner",
    });
  });

  it("shows memory as provided only when the runner projection event exists", () => {
    expect(resolveMemoryRecallPresentation([
      { id: 1, type: "memory.recall.completed", data: { candidate_count: 2 } },
      { id: 2, type: "memory.recall.projected", data: { candidate_count: 2 } },
    ], 0)).toEqual({
      status: "used",
      title: "已提供长期记忆",
      description: "2 条相关记忆已交付本次运行",
    });
  });

  it("distinguishes empty recall from a failed recall", () => {
    expect(resolveMemoryRecallPresentation([{ id: 1, type: "memory.recall.empty" }], 0).status).toBe("empty");
    expect(resolveMemoryRecallPresentation([{ id: 1, type: "memory.recall.failed" }], 0).status).toBe("failed");
  });

  it("keeps context token evidence as a compatibility fallback", () => {
    expect(resolveMemoryRecallPresentation([], 12).status).toBe("used");
    expect(resolveMemoryRecallPresentation([], 0).status).toBe("unused");
  });
});

describe("ChatRunPanel session scope", () => {
  it("does not show another session's latest run for a new empty session", async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [
          {
            id: "run-from-previous-session",
            agentId: "agent-1",
            sessionId: "session-previous",
            traceId: "trace-previous",
            model: "model-1",
            status: "COMPLETED",
          },
        ],
      }),
    } as Response);

    render(
      <ChatRunPanel
        agentId="agent-1"
        sessionId="session-new"
        onOpenTrace={() => undefined}
        onClose={() => undefined}
      />,
    );

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/runs?sessionId=session-new"));
    expect(screen.getByText("发送一条消息后，这里会显示执行位置、用量与事件时间线。")).toBeInTheDocument();
    expect(screen.queryByTitle("run-from-previous-session")).not.toBeInTheDocument();
  });
});
