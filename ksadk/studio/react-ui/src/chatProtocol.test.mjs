import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transformWithOxc } from "vite";

async function loadChatProtocol() {
  let source;
  try {
    source = await readFile(new URL("./chatProtocol.ts", import.meta.url), "utf8");
  } catch (error) {
    assert.fail(`chatProtocol.ts must own the Responses stream: ${error.message}`);
  }
  const transformed = await transformWithOxc(source, "chatProtocol.ts", { lang: "ts" });
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString("base64")}`;
  return import(moduleUrl);
}

test("decodes optional Runtime v2 goal loop and plan capabilities", async () => {
  const chat = await loadChatProtocol();
  const native = { supported: true, mode: "native" };
  const matrix = chat.decodeCapabilityMatrix({
    schema_version: 1,
    cancel: native,
    pause: native,
    resume: native,
    submit_interaction: native,
    attach: native,
    steer: native,
    inject: native,
    checkpoint: native,
    durable_restore: native,
    goal: native,
    loop: native,
    plan: native,
  });

  assert.equal(matrix.goal.supported, true);
  assert.equal(matrix.loop.mode, "native");
  assert.equal(matrix.plan.mode, "native");

  const legacyWireMatrix = {
    schema_version: 1,
    cancel: native,
    pause: native,
    resume: native,
    submit_interaction: native,
    attach: native,
    steer: native,
    inject: native,
    checkpoint: native,
    durable_restore: native,
  };
  const legacyMatrix = chat.decodeCapabilityMatrix(legacyWireMatrix);
  assert.equal(legacyMatrix.goal, undefined);
  assert.equal(legacyMatrix.loop, undefined);
  assert.equal(legacyMatrix.plan, undefined);
});

test("parses fragmented Responses SSE and accumulates reasoning plus output", async () => {
  const chat = await loadChatProtocol();
  const events = [];
  const parser = chat.createResponseSseParser(event => events.push(event));
  parser.push("event: response.created\ndata: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_1\"}}\n");
  parser.push("\nevent: response.reasoning_summary_text.delta\ndata: {\"type\":\"response.reasoning_summary_text.delta\",\"delta\":\"先分析\"}\n\n");
  parser.push(": keep-alive\n\nevent: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"答案\"}\n\n");
  parser.push("event: response.output_item.added\ndata: {\"type\":\"response.output_item.added\",\"item\":{\"id\":\"call_1\",\"type\":\"shell_call\",\"action\":{\"commands\":[\"rg TODO\"]},\"status\":\"in_progress\"}}\n\n");
  parser.push("event: response.output_item.done\ndata: {\"type\":\"response.output_item.done\",\"item\":{\"id\":\"call_1\",\"type\":\"shell_call\",\"action\":{\"commands\":[\"rg TODO\"]},\"status\":\"completed\",\"exit_code\":0}}\n\n");
  parser.push("event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"metadata\":{\"runtime_run_id\":\"run_1\"}}}\n\n");
  parser.finish();

  const state = events.reduce(chat.reduceChatStreamEvent, chat.createChatStreamState("resp_local", "ses_1"));
  assert.equal(state.responseId, "resp_1");
  assert.equal(state.reasoning, "先分析");
  assert.equal(state.output, "答案");
  assert.equal(state.runId, "run_1");
  assert.equal(state.status, "completed");
  assert.deepEqual(state.activities.map(item => [item.kind, item.status, item.title]), [
    ["command", "completed", "rg TODO"],
  ]);
});

test("groups persisted runs into newest-first sessions for one agent", async () => {
  const chat = await loadChatProtocol();
  const sessions = chat.groupRunsBySession([
    { id: "run-a1", agentId: "agent-a", sessionId: "ses-old", input: "旧问题", startedAt: "2026-08-09T08:00:00Z" },
    { id: "run-b", agentId: "agent-b", sessionId: "ses-other", input: "忽略", startedAt: "2026-08-10T10:00:00Z" },
    { id: "run-a2", agentId: "agent-a", sessionId: "ses-new", input: "新问题", startedAt: "2026-08-10T09:00:00Z" },
    { id: "run-a3", agentId: "agent-a", sessionId: "ses-new", input: "追问", startedAt: "2026-08-10T09:05:00Z" },
  ], "agent-a");

  assert.deepEqual(sessions.map(item => item.id), ["ses-new", "ses-old"]);
  assert.equal(sessions[0].title, "新问题");
  assert.deepEqual(sessions[0].runs.map(item => item.id), ["run-a2", "run-a3"]);
});

test("recovers the latest persisted running run after a page refresh", async () => {
  const chat = await loadChatProtocol();
  assert.equal(chat.latestRunningRun([
    { id: "run-old", status: "RUNNING", startedAt: "2026-08-10T09:00:00Z" },
    { id: "run-done", status: "COMPLETED", startedAt: "2026-08-10T09:05:00Z" },
    { id: "run-live", status: "RUNNING", startedAt: "2026-08-10T09:10:00Z" },
  ])?.id, "run-live");
  assert.equal(chat.latestRunningRun([
    { id: "run-done", status: "COMPLETED" },
  ]), undefined);
});

test("treats paused and input-required runs as recoverable active work", async () => {
  const chat = await loadChatProtocol();
  assert.equal(chat.latestActiveRun([
    { id: "run-running", status: "RUNNING", startedAt: "2026-08-10T09:00:00Z" },
    { id: "run-paused", status: "PAUSED", startedAt: "2026-08-10T09:10:00Z" },
    { id: "run-waiting", status: "WAITING_INPUT", startedAt: "2026-08-10T09:20:00Z" },
  ])?.id, "run-waiting");

  const [pausedSession] = chat.groupRunsBySession([
    {
      id: "run-paused",
      agentId: "agent-a",
      sessionId: "ses-paused",
      input: "暂停测试",
      status: "PAUSED",
      startedAt: "2026-08-10T09:10:00Z",
    },
  ], "agent-a");
  assert.equal(pausedSession.activeStatus, "PAUSED");
  assert.deepEqual(
    chat.persistedRunsForDisplay(pausedSession.runs, "run-paused").map(run => run.id),
    [],
  );
});

test("reduces streamed A2UI operations and interaction state without React coupling", async () => {
  const chat = await loadChatProtocol();
  let state = chat.createChatStreamState("resp-a2ui", "ses-a2ui");
  state = chat.reduceChatStreamEvent(state, {
    type: "a2ui.surface.begin",
    runId: "run-a2ui",
    surfaceId: "surface-1",
    a2uiOperations: [
      { version: "v0.9", createSurface: { surfaceId: "surface-1", catalogId: "catalog-1" } },
      { version: "v0.9", updateComponents: { surfaceId: "surface-1", components: [
        { id: "root", component: "Card", title: "需要确认", children: ["approval"] },
        { id: "approval", component: "ApprovalBar", approve_label: "批准", deny_label: "拒绝" },
      ] } },
    ],
  });
  state = chat.reduceChatStreamEvent(state, {
    type: "a2ui.interaction",
    runId: "run-a2ui",
    surfaceId: "surface-1",
    interactionId: "approval-1",
    kind: "approval",
    inputSchema: { type: "object" },
  });

  assert.equal(state.runId, "run-a2ui");
  assert.equal(state.status, "waiting_input");
  assert.equal(state.surfaces[0].id, "surface-1");
  assert.equal(state.surfaces[0].components.approval.component, "ApprovalBar");
  assert.equal(state.surfaces[0].interaction.id, "approval-1");

  state = chat.reduceChatStreamEvent(state, {
    type: "a2ui.action",
    surfaceId: "surface-1",
    interactionId: "approval-1",
    name: "approve",
  });
  assert.equal(state.status, "streaming");
  assert.equal(state.surfaces[0].interaction.status, "resolved");
});

test("projects persisted A2UI operations for refresh replay", async () => {
  const chat = await loadChatProtocol();
  const surfaces = chat.projectA2UISurfaces([
    { id: 1, type: "a2ui.surface.begin", data: {
      surfaceId: "surface-1",
      a2uiOperations: [
        { version: "v0.9", createSurface: { surfaceId: "surface-1", catalogId: "catalog-1" } },
        { version: "v0.9", updateDataModel: { surfaceId: "surface-1", path: "/", value: { selected: ["a"] } } },
      ],
    } },
    { id: 2, type: "a2ui.interaction", data: {
      surfaceId: "surface-1", interactionId: "interaction-1", kind: "multi_select",
    } },
  ]);
  assert.deepEqual(surfaces[0].dataModel, { selected: ["a"] });
  assert.equal(surfaces[0].interaction.kind, "multi_select");
});

test("derives an honest context ring from the latest reported input usage", async () => {
  const chat = await loadChatProtocol();
  assert.equal(typeof chat.contextUsageState, "function");
  assert.deepEqual(chat.contextUsageState(8192, 32768), {
    known: true,
    usedTokens: 8192,
    limitTokens: 32768,
    percent: 25,
  });
  assert.deepEqual(chat.contextUsageState(undefined, 32768), {
    known: false,
    usedTokens: 0,
    limitTokens: 32768,
    percent: 0,
  });
  assert.equal(chat.contextUsageState(40000, 32768).percent, 100);
  assert.equal(chat.latestReportedInputTokens([
    { usage: { inputTokens: 2048, reported: true } },
    { usage: { inputTokens: 0, reported: false } },
  ]), 2048);
  assert.equal(chat.latestReportedInputTokens([
    { usage: { inputTokens: 0, reported: false } },
  ]), undefined);
  assert.deepEqual(chat.contextUsageTooltip(chat.contextUsageState(4481, 32000)), {
    title: "上下文窗口",
    value: "14% 已用",
    detail: "已用 4,481 tokens，共 32,000",
  });
  assert.deepEqual(chat.contextUsageTooltip(chat.contextUsageState(undefined, 32000)), {
    title: "上下文窗口",
    value: "用量未上报",
    detail: "上限 32,000 tokens",
  });
});

test("projects command, tool, and approval events into readable activity cards", async () => {
  const chat = await loadChatProtocol();
  const cards = chat.projectRunActivities([
    { id: 1, type: "thinking.delta", data: { text: "检查上下文" } },
    { id: 2, type: "command.started", data: { command: "rg TODO", callId: "c1" } },
    { id: 3, type: "command.completed", data: { callId: "c1", exitCode: 0 } },
    { id: 4, type: "tool.started", data: { name: "search", callId: "t1" } },
    { id: 5, type: "approval.requested", data: { kind: "workspace-write" } },
    { id: 6, type: "message.delta", data: { text: "恢复后的" } },
    { id: 7, type: "message.delta", data: { text: "流式正文" } },
    { id: 8, type: "tool.completed", data: { callId: "t1", error: "upstream rejected" } },
  ]);

  assert.equal(cards.reasoning, "检查上下文");
  assert.deepEqual(cards.activities.map(item => [item.kind, item.status]), [
    ["command", "completed"],
    ["tool", "failed"],
    ["approval", "waiting"],
  ]);
  assert.equal(cards.activities[0].title, "rg TODO");
  assert.equal(cards.activities[1].title, "search");
  assert.equal(cards.activities[1].detail, "upstream rejected");
  assert.equal(cards.output, "恢复后的流式正文");
});

test("compacts persisted run events into a restrained inspector timeline", async () => {
  const chat = await loadChatProtocol();
  assert.equal(typeof chat.projectRunInspectorTimeline, "function");

  const timeline = chat.projectRunInspectorTimeline([
    { id: 1, type: "run.created", data: { model: "glm-5.2" }, createdAt: "2026-08-10T10:00:00Z" },
    { id: 2, type: "run.started", data: { runtimeType: "codex" }, createdAt: "2026-08-10T10:00:01Z" },
    { id: 3, type: "thinking.delta", data: { text: "检查" }, createdAt: "2026-08-10T10:00:02Z" },
    { id: 4, type: "thinking.delta", data: { text: "上下文" }, createdAt: "2026-08-10T10:00:03Z" },
    { id: 5, type: "tool.started", data: { name: "search", callId: "tool-1" }, createdAt: "2026-08-10T10:00:04Z" },
    { id: 6, type: "tool.completed", data: { callId: "tool-1", result: "2 results" }, createdAt: "2026-08-10T10:00:05Z" },
    { id: 7, type: "message.delta", data: { text: "连接" }, createdAt: "2026-08-10T10:00:06Z" },
    { id: 8, type: "message.completed", data: { text: "连接成功" }, createdAt: "2026-08-10T10:00:07Z" },
    { id: 9, type: "usage.reported", data: { input_tokens: 4481, output_tokens: 6, total_tokens: 4487 }, createdAt: "2026-08-10T10:00:08Z" },
    { id: 10, type: "run.completed", data: { duration_ms: 1537 }, createdAt: "2026-08-10T10:00:09Z" },
  ]);

  assert.deepEqual(timeline.map(item => [item.kind, item.status]), [
    ["run", "running"],
    ["thinking", "completed"],
    ["tool", "completed"],
    ["message", "completed"],
    ["usage", "completed"],
    ["run", "completed"],
  ]);
  assert.equal(timeline[1].detail, "检查上下文");
  assert.equal(timeline[2].title, "search");
  assert.equal(timeline[2].detail, "2 results");
  assert.equal(timeline[3].detail, "连接成功");
  assert.equal(timeline[4].summary, "4,487 tokens");
  assert.equal(timeline.some(item => item.title === "Run 创建"), false);
});

test("projects two completed message items without replacing the first item", async () => {
  const chat = await loadChatProtocol();
  const twoMessageItemsFixture = [
    { id: 1, type: "message.delta", data: { runId: "r1", scopeId: "s1", itemId: "msg-1", partId: "text-0", operation: "append", text: "fir" } },
    { id: 2, type: "message.delta", data: { runId: "r1", scopeId: "s1", itemId: "msg-1", partId: "text-0", operation: "append", text: "st" } },
    { id: 3, type: "message.completed", data: { runId: "r1", scopeId: "s1", itemId: "msg-1", partId: "text-0", text: "first" } },
    { id: 4, type: "message.delta", data: { runId: "r1", scopeId: "s1", itemId: "msg-2", partId: "text-0", operation: "append", text: "second" } },
    { id: 5, type: "message.completed", data: { runId: "r1", scopeId: "s1", itemId: "msg-2", partId: "text-0", text: "second" } },
  ];
  const projection = chat.projectRunActivities(twoMessageItemsFixture);
  assert.deepEqual(projection.textItems.map(item => item.text), ["first", "second"]);
  assert.deepEqual(projection.textItems.map(item => item.itemId), ["msg-1", "msg-2"]);
  assert.deepEqual(projection.textItems.map(item => item.completed), [true, true]);
});

test("keeps identical-text message items as distinct indexed entries", async () => {
  const chat = await loadChatProtocol();
  const projection = chat.projectRunActivities([
    { id: 1, type: "message.completed", data: { runId: "r1", scopeId: "s1", itemId: "msg-a", partId: "text-0", text: "same" } },
    { id: 2, type: "message.completed", data: { runId: "r1", scopeId: "s1", itemId: "msg-b", partId: "text-0", text: "same" } },
  ]);
  assert.equal(projection.textItems.length, 2);
  assert.deepEqual(projection.textItems.map(item => item.itemId), ["msg-a", "msg-b"]);
  assert.deepEqual(projection.textItems.map(item => item.text), ["same", "same"]);
});

test("applies append and replace operations explicitly per item identity", async () => {
  const chat = await loadChatProtocol();
  const projection = chat.projectRunActivities([
    { id: 1, type: "message.delta", data: { runId: "r1", scopeId: "s1", itemId: "m1", partId: "text-0", operation: "append", text: "hello " } },
    { id: 2, type: "message.delta", data: { runId: "r1", scopeId: "s1", itemId: "m1", partId: "text-0", operation: "append", text: "world" } },
    { id: 3, type: "message.delta", data: { runId: "r1", scopeId: "s1", itemId: "m1", partId: "text-0", operation: "replace", text: "rewritten" } },
  ]);
  assert.equal(projection.textItems.length, 1);
  assert.equal(projection.textItems[0].text, "rewritten");
  assert.equal(projection.textItems[0].completed, false);
});

test("derives aggregate output from terminal output refs, not per-run accumulation", async () => {
  const chat = await loadChatProtocol();
  const projection = chat.projectRunActivities([
    { id: 1, type: "message.completed", data: { runId: "r1", scopeId: "s1", itemId: "msg-1", partId: "text-0", text: "alpha" } },
    { id: 2, type: "message.completed", data: { runId: "r1", scopeId: "s1", itemId: "msg-2", partId: "text-0", text: "beta" } },
    { id: 3, type: "run.completed", data: { runtimeEvent: { output_refs: [
      { scope_id: "s1", item_id: "msg-2", part_id: null },
      { scope_id: "s1", item_id: "msg-1", part_id: null },
    ] } } },
  ]);
  assert.equal(projection.output, "beta\n\nalpha");
});

test("replays the cross-language golden projection fixture into item-aware text items", async () => {
  const chat = await loadChatProtocol();
  const fixtureUrl = new URL("../../../../tests/events/fixtures/runtime_projection_golden.json", import.meta.url);
  const golden = JSON.parse(await readFile(fixtureUrl, "utf8"));

  // Map the canonical golden events into Studio's persisted event view (text + terminal refs only).
  const studioEvents = [];
  for (const event of golden.events) {
    const identity = { runId: event.run_id, scopeId: event.scope_id, itemId: event.item_id };
    if (event.event_type === "item.updated" && (event.item_kind === "message" || event.item_kind === "reasoning")) {
      studioEvents.push({
        id: event.seq,
        type: event.item_kind === "reasoning" ? "thinking.delta" : "message.delta",
        data: { ...identity, partId: event.update?.part_id, operation: event.op, text: event.update?.text || "" },
      });
    } else if (event.event_type === "item.completed" && (event.item_kind === "message" || event.item_kind === "reasoning")) {
      const part = (event.snapshot?.parts || []).find(p => p.content_type === "text") || {};
      studioEvents.push({
        id: event.seq,
        type: event.item_kind === "reasoning" ? "thinking.completed" : "message.completed",
        data: { ...identity, partId: part.part_id, text: part.text || "" },
      });
    } else if (event.event_type === "run.completed") {
      studioEvents.push({ id: event.seq, type: "run.completed", data: { runtimeEvent: event } });
    }
  }

  const projection = chat.projectRunActivities(studioEvents);
  const messages = projection.textItems.filter(item => item.kind === "message");
  // Two distinct message items carry identical text and must not collapse into one.
  assert.deepEqual(messages.map(item => item.itemId), ["msg-legal-1", "msg-legal-2"]);
  assert.deepEqual(messages.map(item => item.text), ["The answer is 42.", "The answer is 42."]);
  assert.deepEqual(messages.map(item => item.completed), [true, true]);
  const reasoning = projection.textItems.find(item => item.kind === "thinking");
  assert.equal(reasoning.text, "Analyzing the question...");
  // Aggregate output comes from the terminal output_refs order.
  assert.equal(projection.output, "The answer is 42.\n\nThe answer is 42.");
});
