import { describe, expect, it } from "vitest";

import {
  createConversationItemState,
  decodeConversationItem,
  decodeConversationSurface,
  projectConversationItems,
  reduceConversationItem,
} from "./conversationProtocol";

function item(overrides: Record<string, unknown> = {}) {
  return {
    apiVersion: "conversation.ksadk.io/v1",
    kindVersion: 1,
    itemId: "item-1",
    sourceEventIds: ["event-1"],
    sessionId: "session-1",
    runId: "run-1",
    kind: "assistant_text",
    operation: "append",
    lifecycle: "streaming",
    visibility: "public",
    payloadSchemaRef: "conversation.item.assistant_text/v1",
    payload: { text: "same text" },
    nativeRef: {},
    ...overrides,
  };
}

describe("Conversation v1 browser contract", () => {
  it("decodes a capability surface without guessing unavailable controls", () => {
    const surface = decodeConversationSurface({
      apiVersion: "conversation.ksadk.io/v1",
      kind: "ConversationSurface",
      surfaceId: "surface-1",
      sessionId: "session-1",
      providerRef: "provider-1",
      inputs: [{ name: "text", mode: "native" }, { name: "goal", mode: "unavailable", reason: "not supported" }],
      outputs: [{ name: "streaming", mode: "native" }],
    });

    expect(surface?.inputs).toHaveLength(2);
    expect(decodeConversationSurface({ ...surface, inputs: [{ name: "text", mode: "maybe" }] })).toBeNull();
  });

  it("preserves equal text from distinct items and ignores reconnect replay", () => {
    const first = decodeConversationItem(item())!;
    const second = decodeConversationItem(item({ itemId: "item-2", sourceEventIds: ["event-2"] }))!;
    let state = createConversationItemState();
    state = reduceConversationItem(state, first);
    const afterFirst = state;
    state = reduceConversationItem(state, first);
    expect(state).toBe(afterFirst);
    state = reduceConversationItem(state, second);

    expect(projectConversationItems(state).output).toBe("same textsame text");
    expect(state.items.map(value => value.itemId)).toEqual(["item-1", "item-2"]);
  });

  it("keeps terminal snapshots monotonic when an older delta reconnects late", () => {
    const completed = decodeConversationItem(item({
      sourceEventIds: ["event-terminal"],
      operation: "completed",
      lifecycle: "completed",
      payload: { text: "final" },
    }))!;
    const stale = decodeConversationItem(item({
      sourceEventIds: ["event-stale"],
      payload: { text: " stale" },
    }))!;
    let state = reduceConversationItem(createConversationItemState(), completed);
    state = reduceConversationItem(state, stale);

    expect(state.items[0].lifecycle).toBe("completed");
    expect(projectConversationItems(state).output).toBe("final");
  });

  it("keeps an interleaved reasoning-tool-answer timeline instead of flattening it", () => {
    const values = [
      item({
        itemId: "thinking-1",
        sourceEventIds: ["event-thinking-1"],
        kind: "reasoning",
        payloadSchemaRef: "conversation.item.reasoning/v1",
        payload: { text: "先检查资料" },
      }),
      item({
        itemId: "tool-1",
        sourceEventIds: ["event-tool-1"],
        kind: "tool_call",
        payloadSchemaRef: "conversation.item.tool-call/v1",
        payload: { tool: "search", args: { query: "KsADK" } },
      }),
      item({
        itemId: "thinking-2",
        sourceEventIds: ["event-thinking-2"],
        kind: "reasoning",
        payloadSchemaRef: "conversation.item.reasoning/v1",
        payload: { text: "整理结果" },
      }),
      item({
        itemId: "answer-1",
        sourceEventIds: ["event-answer-1"],
        kind: "assistant_text",
        payload: { text: "这是结论。" },
      }),
    ].map(value => decodeConversationItem(value)!);

    let state = createConversationItemState();
    for (const value of values) state = reduceConversationItem(state, value);

    expect(projectConversationItems(state).timeline.map(value => value.item.itemId)).toEqual([
      "thinking-1", "tool-1", "thinking-2", "answer-1",
    ]);
  });

  it("coalesces a separate tool result into its original call card by callId", () => {
    const call = decodeConversationItem(item({
      itemId: "tool-call-item",
      sourceEventIds: ["event-tool-call"],
      kind: "tool_call",
      payloadSchemaRef: "conversation.item.tool-call/v1",
      payload: { callId: "call-1", tool: "search", args: { query: "KsADK" } },
    }))!;
    const result = decodeConversationItem(item({
      itemId: "tool-result-item",
      sourceEventIds: ["event-tool-result"],
      kind: "tool_call",
      operation: "completed",
      lifecycle: "completed",
      payloadSchemaRef: "conversation.item.tool-call/v1",
      payload: { callId: "call-1", output: { hits: 3 }, isError: false },
    }))!;

    let state = reduceConversationItem(createConversationItemState(), call);
    state = reduceConversationItem(state, result);
    const [entry] = projectConversationItems(state).timeline;

    expect(projectConversationItems(state).timeline).toHaveLength(1);
    expect(entry).toMatchObject({
      key: "tool:call-1",
      sourceItemIds: ["tool-call-item", "tool-result-item"],
      item: expect.objectContaining({
        itemId: "tool-call-item",
        lifecycle: "completed",
        payload: expect.objectContaining({ tool: "search", output: { hits: 3 } }),
      }),
    });
  });

  it("renders artifacts safely and hides unknown kinds while degrading unknown schemas to cards", () => {
    const unknownKind = decodeConversationItem(item({
      itemId: "future",
      sourceEventIds: ["event-future"],
      kind: "game_board",
      payloadSchemaRef: "vendor.game-board/v7",
      payload: { jsx: "<script>bad()</script>" },
    }))!;
    const unknownSchema = decodeConversationItem(item({
      itemId: "future-text",
      sourceEventIds: ["event-future-text"],
      payloadSchemaRef: "conversation.item.assistant_text/v99",
    }))!;
    const artifact = decodeConversationItem(item({
      itemId: "artifact",
      sourceEventIds: ["event-artifact"],
      kind: "artifact",
      operation: "completed",
      lifecycle: "completed",
      payloadSchemaRef: "conversation.item.artifact/v1",
      payload: { name: "report.md", mimeType: "text/markdown", uri: "javascript:alert(1)" },
    }))!;
    let state = createConversationItemState();
    for (const value of [unknownKind, unknownSchema, artifact]) {
      state = reduceConversationItem(state, value);
    }
    const presentation = projectConversationItems(state);

    // Unknown runtime kinds are hidden to match the Python projector and
    // avoid spamming the conversation with "暂不支持的内容" cards on every
    // turn when a provider emits an additive item kind the Studio does not
    // yet render.
    expect(unknownKind?.visibility).toBe("hidden");
    expect(presentation.fallbacks).toEqual([
      expect.objectContaining({ id: "future-text" }),
    ]);
    expect(presentation.artifacts).toEqual([
      { id: "artifact", name: "report.md", mimeType: "text/markdown", uri: null },
    ]);
  });
});
