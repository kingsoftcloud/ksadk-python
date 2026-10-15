import { describe, expect, it } from "vitest";
import { extractLiveThinking } from "./liveThinking";

describe("live thinking projection", () => {
  it("returns nothing for the transcript or compacted view", () => {
    const messages = [{ id: "m", role: "model" as const, content: "结果", timestamp: 0, invocationId: "run-1",
      reasoning: "详细推理", blocks: [{ id: "t", type: "thinking" as const, content: "详细推理", status: "done" as const }] }];
    expect(extractLiveThinking(messages, { streaming: false })).toBeNull();
  });

  it("appends the streaming reasoning blocks of the live run in order", () => {
    const view = extractLiveThinking([
      { id: "user", role: "user", content: "问题", timestamp: 0 },
      { id: "a", role: "model", content: "", timestamp: 1, invocationId: "run-1",
        blocks: [{ id: "t1", type: "thinking", content: "第一步", status: "done" }] },
      { id: "b", role: "model", content: "", timestamp: 2, invocationId: "run-1",
        blocks: [{ id: "t2", type: "thinking", content: "第二步", status: "streaming" }] },
    ], { streaming: true });
    expect(view).toMatchObject({ runId: "run-1", text: "第一步第二步", streaming: true });
  });

  it("stops the pulse once thinking is closed or the answer starts", () => {
    const messages = [
      { id: "a", role: "model" as const, content: "", timestamp: 1, invocationId: "run-1",
        blocks: [{ id: "t1", type: "thinking" as const, content: "推理", status: "done" as const }] },
    ];
    expect(extractLiveThinking(messages, { streaming: true })).toMatchObject({ streaming: false });
    expect(extractLiveThinking([
      { id: "a", role: "model", content: "", timestamp: 1, invocationId: "run-1",
        blocks: [{ id: "t1", type: "thinking", content: "推理", status: "streaming" }] },
      { id: "b", role: "model", content: "答案", timestamp: 2, invocationId: "run-1" },
    ], { streaming: true })).toMatchObject({ streaming: false, text: "推理" });
  });

  it("does not merge a previous run's reasoning into the live turn", () => {
    const view = extractLiveThinking([
      { id: "old", role: "model", content: "", timestamp: 1, invocationId: "run-old",
        blocks: [{ id: "t0", type: "thinking", content: "上一轮", status: "done" }] },
      { id: "live", role: "model", content: "", timestamp: 2, invocationId: "run-new",
        blocks: [{ id: "t1", type: "thinking", content: "本轮", status: "streaming" }] },
    ], { streaming: true });
    expect(view).toMatchObject({ runId: "run-new", text: "本轮" });
  });

  it("falls back to the reasoning field when the host omits ordered blocks", () => {
    expect(extractLiveThinking([
      { id: "m", role: "model", content: "", timestamp: 1, invocationId: "run-1", reasoning: "仅 reasoning 字段" },
    ], { streaming: true })).toMatchObject({ text: "仅 reasoning 字段", streaming: true });
    expect(extractLiveThinking([
      { id: "m", role: "model", content: "", timestamp: 1, invocationId: "run-1", reasoning: "" },
    ], { streaming: true })).toBeNull();
  });
});
