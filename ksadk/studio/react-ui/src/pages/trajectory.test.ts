import { describe, expect, it } from "vitest";
import {
  mergeTrajectory,
  prependTrajectory,
  trajectoryState,
  type TrajectoryEvent,
} from "./trajectory";

function event(
  seqId: number,
  recordId = `event:${seqId}`,
  status: string | null = null,
): TrajectoryEvent {
  return {
    projectionVersion: 1,
    seqId,
    eventId: `evt-${seqId}`,
    recordId,
    type: "run.progress",
    category: "system",
    turnId: null,
    stepId: null,
    timestamp: seqId,
    status,
    durationMs: null,
    summary: `event ${seqId}`,
    details: {},
  };
}

describe("trajectory reducer", () => {
  it("deduplicates events and reports a cursor gap", () => {
    const initial = trajectoryState([
      event(7, "tool:call-1", "running"),
      event(8),
    ]);

    expect(mergeTrajectory(initial, event(8)).items).toHaveLength(2);
    expect(mergeTrajectory(initial, event(10)).gap).toEqual({
      afterSeqId: 8,
      beforeSeqId: 10,
    });
  });

  it("folds begin and end events into one stable record", () => {
    const initial = trajectoryState([event(7, "tool:call-1", "running")]);
    const next = mergeTrajectory(initial, event(8, "tool:call-1", "completed"));

    expect(next.items).toHaveLength(1);
    expect(next.items[0].status).toBe("completed");
    expect(next.items[0].recordId).toBe("tool:call-1");
  });

  it("accumulates streaming deltas into one stable record", () => {
    const first = {
      ...event(7, "text:turn-1:commentary"),
      type: "text.delta",
      summary: "过程输出",
      details: { text: "你" },
    };
    const second = {
      ...event(8, "text:turn-1:commentary"),
      type: "text.delta",
      summary: "过程输出",
      details: { text: "好" },
    };

    const next = mergeTrajectory(trajectoryState([first]), second);

    expect(next.items).toHaveLength(1);
    expect(next.items[0].details.text).toBe("你好");
  });

  it("uses completed text instead of duplicating accumulated deltas", () => {
    const first = {
      ...event(7, "text:turn-1:final_answer"),
      type: "text.delta",
      summary: "最终回答",
      details: { text: "你" },
    };
    const completed = {
      ...event(8, "text:turn-1:final_answer", "completed"),
      type: "text.completed",
      summary: "最终回答",
      details: { text: "你好" },
    };

    const next = mergeTrajectory(trajectoryState([first]), completed);

    expect(next.items).toHaveLength(1);
    expect(next.items[0].details.text).toBe("你好");
  });

  it("replaces accumulated text when a delta carries replace semantics", () => {
    const first = {
      ...event(7, "assistant:step-1"),
      type: "text.delta",
      category: "assistant" as const,
      details: { text: "旧内容" },
    };
    const replacement = {
      ...event(8, "assistant:step-1"),
      type: "text.delta",
      category: "assistant" as const,
      details: { text: "新内容", replace: true },
    };

    expect(trajectoryState([first, replacement]).items[0].details.output).toBe("新内容");
  });

  it("treats cancelled as a terminal status", () => {
    const running = event(7, "system:turn-1", "running");
    const cancelled = event(8, "system:turn-1", "cancelled");

    expect(trajectoryState([running, cancelled]).items[0].status).toBe("cancelled");
  });

  it("folds step message facts into reasoning, output, and usage", () => {
    const message = "assistant:step-1";
    const events = [
      {
        ...event(7, message, "running"),
        type: "model.call.begin",
        category: "assistant" as const,
        summary: "Message",
        turnId: "turn-1",
        stepId: "step-1",
        details: { model: "demo-model", model_call_id: "model-1" },
      },
      {
        ...event(8, message),
        type: "reasoning.delta",
        category: "assistant" as const,
        summary: "Message",
        turnId: "turn-1",
        stepId: "step-1",
        details: { text: "先分析" },
      },
      {
        ...event(9, message, "completed"),
        type: "text.completed",
        category: "assistant" as const,
        summary: "Message",
        turnId: "turn-1",
        stepId: "step-1",
        details: { text: "答案" },
      },
      {
        ...event(10, message),
        type: "usage.reported",
        category: "assistant" as const,
        summary: "Message",
        turnId: "turn-1",
        stepId: "step-1",
        details: { input_tokens: 3, output_tokens: 2, reasoning_tokens: 1, total_tokens: 6 },
      },
    ];

    const next = trajectoryState(events);

    expect(next.items).toHaveLength(1);
    expect(next.items[0].type).toBe("assistant.message");
    expect(next.items[0].details.reasoning).toBe("先分析");
    expect(next.items[0].details.output).toBe("答案");
    expect(next.items[0].details.usage).toEqual({
      input_tokens: 3,
      output_tokens: 2,
      reasoning_tokens: 1,
      total_tokens: 6,
    });
    expect(next.items[0].sourceEvents.map((item) => item.seqId)).toEqual([7, 8, 9, 10]);
    expect(next.items[0].firstSeqId).toBe(7);
    expect(next.items[0].lastSeqId).toBe(10);
  });

  it("keeps tool arguments when the result settles the record", () => {
    const begin = {
      ...event(7, "tool:call-1", "running"),
      type: "tool.call.begin",
      category: "tool" as const,
      details: { name: "search", args: { q: "docs" } },
    };
    const end = {
      ...event(8, "tool:call-1", "completed"),
      type: "tool.call.end",
      category: "tool" as const,
      details: { name: "search", result: { count: 2 } },
    };

    const next = trajectoryState([begin, end]);

    expect(next.items[0].details.args).toEqual({ q: "docs" });
    expect(next.items[0].details.result).toEqual({ count: 2 });
  });

  it("applies cached events after a cursor gap is filled", () => {
    const initial = trajectoryState([event(8)]);
    const waiting = mergeTrajectory(initial, event(10));
    const filled = mergeTrajectory(waiting, event(9));

    expect(filled.gap).toBeNull();
    expect(filled.lastSeqId).toBe(10);
    expect([...filled.bySeq.keys()]).toEqual([8, 10, 9]);
    expect(filled.items.map((item) => item.seqId)).toEqual([8, 9, 10]);
  });

  it("accepts sparse session cursors when the server filters an invocation", () => {
    const initial = trajectoryState([event(7)]);
    const next = mergeTrajectory(initial, event(10), false);

    expect(next.gap).toBeNull();
    expect(next.lastSeqId).toBe(10);
    expect(next.items.map((item) => item.seqId)).toEqual([7, 10]);
  });

  it("prepends older pages without changing existing record positions", () => {
    const initial = trajectoryState([
      event(7, "tool:call-1", "completed"),
      event(8),
    ]);
    const next = prependTrajectory(
      initial,
      [event(5), event(6), event(7, "tool:call-1", "running")],
      false,
    );

    expect(next.items.map((item) => item.recordId)).toEqual([
      "event:5",
      "event:6",
      "tool:call-1",
      "event:8",
    ]);
    expect(next.items[2].status).toBe("completed");
    expect(next.hasMore).toBe(false);
  });

  it("prepends older deltas to a stream that crosses page boundaries", () => {
    const latest = {
      ...event(8, "text:turn-1:commentary"),
      type: "text.delta",
      summary: "过程输出",
      details: { text: "好" },
    };
    const older = {
      ...event(7, "text:turn-1:commentary"),
      type: "text.delta",
      summary: "过程输出",
      details: { text: "你" },
    };

    const next = prependTrajectory(trajectoryState([latest]), [older], false);

    expect(next.items).toHaveLength(1);
    expect(next.items[0].details.text).toBe("你好");
  });

  it("updates record content without reordering the ledger", () => {
    const initial = trajectoryState([
      event(7, "tool:call-1", "running"),
      event(8, "event:middle"),
    ]);
    const next = mergeTrajectory(initial, event(9, "tool:call-1", "failed"));

    expect(next.items.map((item) => item.recordId)).toEqual([
      "tool:call-1",
      "event:middle",
    ]);
    expect(next.items[0].seqId).toBe(9);
    expect(next.items[0].status).toBe("failed");
  });

  it("produces the same semantic records for replay and incremental events", () => {
    const begin = {
      ...event(7, "tool:call-1", "running"),
      type: "tool.call.begin",
      category: "tool" as const,
      timestamp: 10,
      details: { name: "search", args: { q: "docs" } },
    };
    const end = {
      ...event(8, "tool:call-1", "completed"),
      type: "tool.call.end",
      category: "tool" as const,
      timestamp: 10.25,
      details: { name: "search", result: "done" },
    };

    const replayed = trajectoryState([begin, end]);
    const streamed = mergeTrajectory(trajectoryState([begin]), end);

    expect(streamed.items).toEqual(replayed.items);
    expect(streamed.items[0].startedAt).toBe(10);
    expect(streamed.items[0].endedAt).toBe(10.25);
    expect(streamed.items[0].durationMs).toBe(250);
  });

  it("does not invent zero duration from one terminal event", () => {
    const completed = {
      ...event(7, "assistant:step-1", "completed"),
      type: "text.completed",
      category: "assistant" as const,
      details: { text: "done" },
    };

    expect(trajectoryState([completed]).items[0].durationMs).toBeNull();
  });
});
