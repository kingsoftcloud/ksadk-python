import { describe, expect, it } from "vitest";
import { resolveMemoryRecallPresentation } from "./ChatRunPanel";

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
