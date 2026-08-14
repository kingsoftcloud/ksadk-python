import { describe, expect, it } from "vitest";
import { resolveMemoryRecallPresentation } from "./ChatRunPanel";

describe("resolveMemoryRecallPresentation", () => {
  it("prioritizes an actual recall event over missing native context token evidence", () => {
    expect(resolveMemoryRecallPresentation([
      {
        id: 1,
        type: "memory.recall.completed",
        data: { candidate_count: 2, provider: "local-default" },
      },
    ], 0)).toEqual({
      status: "used",
      title: "已使用长期记忆",
      description: "已召回 2 条与当前问题相关的记忆",
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
