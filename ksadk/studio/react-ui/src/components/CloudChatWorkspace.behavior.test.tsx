import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch, showToast } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("../api", () => ({ apiFetch }));
vi.mock("./Toast", () => ({ showToast }));

import { CloudChatWorkspace } from "./CloudChatWorkspace";

const base = "/api/v1/deployments/dep-cloud/cloud-chat";

function jsonResponse(value: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("CloudChatWorkspace cloud-session behavior", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const values = new Map<string, string>();
    const storage: Storage = {
      get length() { return values.size; },
      clear: () => values.clear(),
      getItem: key => values.get(key) ?? null,
      key: index => [...values.keys()][index] ?? null,
      removeItem: key => { values.delete(key); },
      setItem: (key, value) => { values.set(key, value); },
    };
    vi.stubGlobal("localStorage", storage);
  });

  it("ignores a slow message response from the previously selected session", async () => {
    let resolveOldMessages!: (response: Response) => void;
    const oldMessages = new Promise<Response>((resolve) => {
      resolveOldMessages = resolve;
    });
    apiFetch.mockImplementation(async (path: string) => {
      if (path === `${base}/sessions`) {
        return jsonResponse({ sessions: [
          { session_id: "sess-old", title: "旧会话" },
          { session_id: "sess-new", title: "新会话" },
        ] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path === `${base}/sessions/sess-old/messages`) return oldMessages;
      if (path === `${base}/sessions/sess-new/messages`) {
        return jsonResponse({ messages: [{
          message_id: "new-message",
          role: "assistant",
          content: "新会话内容",
        }] });
      }
      if (path.endsWith("/events?limit=1000")) return jsonResponse({ events: [] });
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);
    await screen.findByText("旧会话");
    await userEvent.click(screen.getByRole("button", { name: /^新会话$/ }));
    expect(await screen.findByText("新会话内容")).toBeInTheDocument();

    resolveOldMessages(jsonResponse({ messages: [{
      message_id: "old-message",
      role: "assistant",
      content: "不应覆盖当前会话",
    }] }));

    await waitFor(() => expect(screen.queryByText("不应覆盖当前会话")).not.toBeInTheDocument());
    expect(screen.getByText("新会话内容")).toBeInTheDocument();
  });

  it("ignores an older response after returning to the same cloud session", async () => {
    let resolveFirstOldMessages!: (response: Response) => void;
    const firstOldMessages = new Promise<Response>((resolve) => {
      resolveFirstOldMessages = resolve;
    });
    let oldMessageReads = 0;
    apiFetch.mockImplementation(async (path: string) => {
      if (path === `${base}/sessions`) {
        return jsonResponse({ sessions: [
          { session_id: "sess-old", title: "旧会话" },
          { session_id: "sess-new", title: "新会话" },
        ] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path === `${base}/sessions/sess-old/messages`) {
        oldMessageReads += 1;
        if (oldMessageReads === 1) return firstOldMessages;
        return jsonResponse({ messages: [{
          message_id: "fresh-old-message",
          role: "assistant",
          content: "回到旧会话后的最新内容",
        }] });
      }
      if (path === `${base}/sessions/sess-new/messages`) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000")) return jsonResponse({ events: [] });
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);
    await screen.findByText("旧会话");
    await userEvent.click(screen.getByRole("button", { name: /^新会话$/ }));
    await userEvent.click(screen.getByRole("button", { name: /^旧会话$/ }));
    expect(await screen.findByText("回到旧会话后的最新内容")).toBeInTheDocument();

    resolveFirstOldMessages(jsonResponse({ messages: [{
      message_id: "stale-old-message",
      role: "assistant",
      content: "过期响应不应覆盖新内容",
    }] }));

    await waitFor(() => {
      expect(screen.queryByText("过期响应不应覆盖新内容")).not.toBeInTheDocument();
    });
    expect(screen.getByText("回到旧会话后的最新内容")).toBeInTheDocument();
  });

  it("keeps terminal sessions quiet and marks only active work with a subtle ring", async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === `${base}/sessions`) {
        return jsonResponse({ sessions: [
          { session_id: "sess-done", title: "已经完成", active_run_status: "completed" },
          { session_id: "sess-running", title: "仍在运行", active_run_status: "running" },
        ] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages")) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000")) return jsonResponse({ events: [] });
      throw new Error(`unexpected request: ${path}`);
    });

    const { container } = render(
      <CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />,
    );

    await screen.findByText("已经完成");
    const doneItem = screen.getByText("已经完成").closest(".chat-session-item");
    const runningItem = screen.getByText("仍在运行").closest(".chat-session-item");
    expect(doneItem?.querySelector(".session-status")).toBeNull();
    expect(doneItem).not.toHaveTextContent(/completed/i);
    expect(runningItem).toHaveClass("running");
    expect(within(runningItem as HTMLElement).getByLabelText("运行中")).toHaveClass("session-status", "running");
    expect(container.querySelectorAll(".session-status")).toHaveLength(1);
  });

  it("renders an assistant delta before the admitted run reaches a terminal state", async () => {
    let eventStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    let directStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const encoder = new TextEncoder();
    const eventStream = new ReadableStream<Uint8Array>({ start(controller) { eventStreamController = controller; } });
    const directStream = new ReadableStream<Uint8Array>({ start(controller) { directStreamController = controller; } });

    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-1", title: "流式会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path === `${base}/sessions/sess-1/messages` && !init?.method) {
        return jsonResponse({ messages: [] });
      }
      if (path === `${base}/sessions/sess-1/events?limit=1000` && !init?.method) {
        return jsonResponse({ events: [{ event_type: "run.completed", seq_id: 5 }] });
      }
      if (path === `${base}/sessions/sess-1/events/stream?afterSeqId=5`) {
        return new Response(eventStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path === `${base}/sessions/sess-1/messages/stream` && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    const user = userEvent.setup();
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("流式会话");
    await user.type(screen.getByRole("textbox", { name: "消息" }), "开始流式回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
    expect(screen.getByText("开始流式回答", { selector: ".message-content p" })).toBeInTheDocument();

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-1/events/stream?afterSeqId=5`,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));

    // SessionEvent can project the same assistant text, but the foreground
    // stream is authoritative and must keep the body single-rendered.
    eventStreamController?.enqueue(encoder.encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"item.updated\",\"seq_id\":6,\"run_id\":\"run-1\","
      + "\"content\":{\"runtime_event\":{\"item_kind\":\"message\",\"op\":\"append\","
      + "\"update\":{\"text\":\"第一段\"}}}}\n\n",
    ));
    directStreamController?.enqueue(encoder.encode(
      "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"第一段\"}}]}\n\n"
      + "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"\\n第二段\"}}]}\n\n",
    ));

    const foregroundReply = await screen.findByLabelText("云端流式回复");
    expect(foregroundReply).toHaveTextContent("第一段");
    expect(foregroundReply).toHaveTextContent("第二段");
    expect(screen.getAllByLabelText("云端流式回复")).toHaveLength(1);
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
    directStreamController?.enqueue(encoder.encode("data: [DONE]\n\n"));
    await waitFor(() => expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument());
  });

  it("renders the legacy bare RunAgent delta frames used by deployed Agents", async () => {
    let directStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const directStream = new ReadableStream<Uint8Array>({ start(controller) { directStreamController = controller; } });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-bare-delta", title: "现网兼容流" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);
    await screen.findByText("现网兼容流");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "验证裸增量");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    directStreamController?.enqueue(new TextEncoder().encode(
      'data: {"delta":"流"}\n\n'
      + 'data: {"delta":"式验证通过。"}\n\n',
    ));

    expect(await screen.findByText("流式验证通过。")).toBeInTheDocument();
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
    directStreamController?.enqueue(new TextEncoder().encode(
      'data: {"id":"resp-legacy","object":"response","status":"completed","output_text":"流式验证通过。"}\n\n',
    ));
    directStreamController?.close();
    await waitFor(() => expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument());
  });

  it("does not block the foreground stream on a second historical event read", async () => {
    let eventReads = 0;
    let streamPosted = false;
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-fast", title: "立即流式" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        eventReads += 1;
        if (eventReads === 1) return jsonResponse({ events: [{ event_type: "run.completed", seq_id: 7 }] });
        return await new Promise<Response>(() => {});
      }
      if (path.endsWith("/events/stream?afterSeqId=7")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        streamPosted = true;
        const body = JSON.parse(String(init.body));
        expect(body).not.toHaveProperty("invocationId");
        return new Response("data: [DONE]\n\n", { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("立即流式");
    await waitFor(() => expect(eventReads).toBe(1));
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "不要等历史读取");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(streamPosted).toBe(true));
  });

  it("admits only one cloud run when send is clicked twice before React rerenders", async () => {
    let directPosts = 0;
    const directStream = new ReadableStream<Uint8Array>({ start() {} });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-single-admission", title: "单次准入" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        directPosts += 1;
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);
    await screen.findByText("单次准入");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "禁止重复提交");

    const send = screen.getByRole("button", { name: "发送消息" });
    // Dispatch both browser events in one task. `sending` has not committed
    // yet, so only the synchronous admission fence can prevent a second run.
    fireEvent.click(send);
    fireEvent.click(send);

    await waitFor(() => expect(directPosts).toBe(1));
    expect(send).toBeDisabled();
  });

  it("projects canonical nested RuntimeEvent items before RunAgent returns", async () => {
    let directStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const directStream = new ReadableStream<Uint8Array>({ start(controller) { directStreamController = controller; } });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-canonical", title: "Canonical 会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        return jsonResponse({ events: [{ event_type: "user_message", seq_id: 10 }] });
      }
      if (path.endsWith("/events/stream?afterSeqId=10")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("Canonical 会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "展示实时过程");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-canonical/events/stream?afterSeqId=10`,
      expect.anything(),
    ));

    const frames = [
      { event_type: "item.started", item_id: "reason-1", item_kind: "reasoning", initial: null },
      { event_type: "item.updated", item_id: "reason-1", item_kind: "reasoning", op: "append", update: { text: "正在分析问题" } },
      { event_type: "item.started", item_id: "tool-1", item_kind: "tool_call", initial: { parts: [{ content_type: "tool_call", name: "web_search", arguments: { query: "weather" } }] } },
      { event_type: "item.completed", item_id: "tool-1", item_kind: "tool_call", snapshot: { parts: [{ content_type: "tool_call", name: "web_search", arguments: { query: "weather" } }] } },
      { event_type: "item.started", item_id: "msg-1", item_kind: "message", initial: null },
      { event_type: "item.updated", item_id: "msg-1", item_kind: "message", op: "append", update: { text: "实时回答第一段" } },
      { event_type: "interaction.requested", interaction_id: "approval-1", interaction_kind: "approval", request: { kind: "tool", title: "允许查询天气" } },
    ];
    const encoded = frames.map((runtimeEvent, index) => (
      "event: session.event\n"
      + `data: ${JSON.stringify({
        event_type: "runtime_event",
        seq_id: 11 + index,
        invocation_id: "inv-qwen",
        // The cloud SessionEvent API accepts both historic snake_case and
        // current camelCase envelopes.  The chat surface must give either
        // form the same canonical ConversationItem treatment.
        content: { runtimeEvent: { ...runtimeEvent, run_id: "run-qwen", scope_id: "scope-qwen" } },
      })}\n\n`
    )).join("");
    directStreamController?.enqueue(new TextEncoder().encode(encoded));

    expect(await screen.findByText("实时回答第一段")).toBeInTheDocument();
    expect(screen.getByText(/正在分析问题/)).toBeInTheDocument();
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "待处理确认" })).toHaveTextContent("允许查询天气");
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
    directStreamController?.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
  });

  it("projects a canonical reasoning delta only once when both cloud streams deliver it", async () => {
    let sessionStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    let directStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const sessionStream = new ReadableStream<Uint8Array>({ start(controller) { sessionStreamController = controller; } });
    const directStream = new ReadableStream<Uint8Array>({ start(controller) { directStreamController = controller; } });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-dedup", title: "双流去重" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response(sessionStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("双流去重");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "不要重复思考");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-dedup/events/stream?afterSeqId=0`,
      expect.anything(),
    ));
    const frame = [
      "event: session.event",
      `data: ${JSON.stringify({
        event_type: "runtime_event",
        seq_id: 1,
        invocation_id: "inv-dedup",
        content: { runtime_event: {
          schema_version: 2,
          event_id: "event-dedup-1",
          run_id: "run-dedup",
          scope_id: "scope-dedup",
          event_type: "item.updated",
          item_id: "reason-dedup",
          item_kind: "reasoning",
          op: "append",
          update: { text: "只显示一次" },
        } },
      })}`,
      "",
      "",
    ].join("\n");
    sessionStreamController?.enqueue(new TextEncoder().encode(frame));
    expect(await screen.findByText("只显示一次")).toBeInTheDocument();
    directStreamController?.enqueue(new TextEncoder().encode(frame));
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(screen.getByText("只显示一次")).toBeInTheDocument();
    expect(screen.queryByText("只显示一次只显示一次")).not.toBeInTheDocument();
    directStreamController?.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
  });

  it("appends implicit item.updated reasoning deltas instead of replacing earlier thought", async () => {
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-reasoning", title: "增量思考" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response([
          'event: item.updated\ndata: {"event_type":"item.updated","run_id":"run-reasoning","item_id":"reason-1","item_kind":"reasoning","update":{"text":"先核对上下文"}}',
          'event: item.updated\ndata: {"event_type":"item.updated","run_id":"run-reasoning","item_id":"reason-1","item_kind":"reasoning","update":{"text":"，再检查工具"}}',
          "data: [DONE]",
          "",
        ].join("\n\n"), { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);
    await screen.findByText("增量思考");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "检查过程");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("先核对上下文，再检查工具")).toBeInTheDocument();
  });

  it("does not pull the user back to the tail after they scroll up during streaming", async () => {
    let directStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const directStream = new ReadableStream<Uint8Array>({ start(controller) { directStreamController = controller; } });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-scroll", title: "滚动会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    const { container } = render(
      <CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />,
    );
    await screen.findByText("滚动会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "生成长回复");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    directStreamController?.enqueue(new TextEncoder().encode(
      'data: {"choices":[{"index":0,"delta":{"content":"第一段"}}]}\n\n',
    ));
    await screen.findByText("第一段");

    const list = container.querySelector(".chat-message-list") as HTMLDivElement;
    Object.defineProperties(list, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    });
    fireEvent.scroll(list);
    directStreamController?.enqueue(new TextEncoder().encode(
      'data: {"choices":[{"index":0,"delta":{"content":"第二段"}}]}\n\n',
    ));
    await screen.findByText("第一段第二段");
    expect(list.scrollTop).toBe(100);
    directStreamController?.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
  });

  it("replays reasoning and tools that precede more than 200 message events", async () => {
    const nestedEvent = (
      seq: number,
      eventType: string,
      runtimeEvent: Record<string, unknown>,
    ) => ({
      seq_id: seq,
      event_type: eventType,
      invocation_id: "inv-long",
      content: {
        runtime_event: {
          event_type: eventType,
          run_id: "run-long",
          scope_id: "scope-long",
          seq,
          ...runtimeEvent,
        },
      },
    });
    const longMessageTail = Array.from({ length: 205 }, (_, index) => nestedEvent(
      index + 5,
      "item.updated",
      {
        item_id: "message-long",
        item_kind: "message",
        op: "append",
        update: { text: `片段-${index}` },
      },
    ));
    const history = [
      nestedEvent(1, "item.started", {
        item_id: "reason-long",
        item_kind: "reasoning",
      }),
      nestedEvent(2, "item.updated", {
        item_id: "reason-long",
        item_kind: "reasoning",
        op: "append",
        update: { text: "刷新后仍保留的思考" },
      }),
      nestedEvent(3, "item.started", {
        item_id: "tool-long",
        item_kind: "tool_call",
        initial: { parts: [{ name: "metaso_web_search", arguments: { q: "金山云" } }] },
      }),
      nestedEvent(4, "item.completed", {
        item_id: "tool-long",
        item_kind: "tool_call",
        snapshot: { parts: [{ name: "metaso_web_search", arguments: { q: "金山云" } }] },
      }),
      ...longMessageTail,
    ];
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-long", title: "长事件会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        return jsonResponse({ events: history, total: history.length });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("长事件会话");
    expect(await screen.findByText(/刷新后仍保留的思考/)).toBeInTheDocument();
    expect(screen.getByText("metaso_web_search")).toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledWith(`${base}/sessions/sess-long/events?limit=1000`);
  });

  it("paginates durable cloud events and preserves their cross-kind order", async () => {
    const nestedEvent = (
      seq: number,
      eventType: string,
      runtimeEvent: Record<string, unknown>,
    ) => ({
      event_type: eventType,
      seq_id: seq,
      content: {
        runtime_event: {
          schema_version: 2,
          event_id: `event-${seq}`,
          seq,
          timestamp: seq,
          run_id: "run-paged",
          scope_id: "scope-paged",
          source: { framework: "codex" },
          event_type: eventType,
          ...runtimeEvent,
        },
      },
    });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-paged", title: "分页会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        return jsonResponse({
          total: 2,
          events: [nestedEvent(1, "item.completed", {
            item_id: "reason-first",
            item_kind: "reasoning",
            snapshot: { parts: [{ text: "先分析" }] },
          })],
        });
      }
      if (path.endsWith("/events?limit=1000&offset=1") && !init?.method) {
        return jsonResponse({
          total: 2,
          events: [nestedEvent(2, "item.completed", {
            item_id: "tool-second",
            item_kind: "tool_call",
            snapshot: { parts: [{ name: "读取版本", output: "Python 3.12" }] },
          })],
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(
      <CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />,
    );
    const reasoning = await screen.findByText("先分析");
    const tool = await screen.findByText("读取版本");
    expect(reasoning.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-paged/events?limit=1000&offset=1`,
    );
  });

  it("uses typed ConversationItems for cloud plans and keeps hidden extensions out of chat", async () => {
    const item = (kind: string, itemId: string, visibility = "public") => ({
      apiVersion: "conversation.ksadk.io/v1",
      kindVersion: 1,
      itemId,
      sourceEventIds: [`event-${itemId}`],
      sessionId: "sess-typed",
      runId: "run-typed",
      kind,
      operation: "replace",
      lifecycle: "completed",
      visibility,
      payloadSchemaRef: kind === "plan" ? "conversation.item.plan/v1" : "conversation.item.unknown/v1",
      payload: kind === "plan" ? { text: "先核对接口，再部署验证" } : { summary: "future extension" },
      nativeRef: {},
    });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-typed", title: "类型化会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        return jsonResponse({ events: [
          { seq_id: 1, conversationItem: item("plan", "plan-1") },
          { seq_id: 2, conversationItem: item("future_extension", "future-1", "hidden") },
        ] });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    expect(await screen.findByText("先核对接口，再部署验证")).toBeInTheDocument();
    expect(screen.queryByText("future extension")).not.toBeInTheDocument();
    expect(screen.queryByText("暂不支持的内容")).not.toBeInTheDocument();
  });

  it("ends foreground waiting when SessionEvent reports an approval interrupt", async () => {
    let eventStreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const eventStream = new ReadableStream<Uint8Array>({ start(controller) { eventStreamController = controller; } });
    const directStream = new ReadableStream<Uint8Array>({ start() {} });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-approval", title: "审批会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response(eventStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("审批会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "执行需要批准的工具");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-approval/messages/stream`,
      expect.objectContaining({ method: "POST" }),
    ));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-approval/events/stream?afterSeqId=0`,
      expect.anything(),
    ));

    eventStreamController?.enqueue(new TextEncoder().encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"interaction.requested\",\"seq_id\":1,\"invocation_id\":\"run-approval\","
      + "\"interaction_id\":\"approval-1\",\"interaction_kind\":\"approval\","
      + "\"request\":{\"kind\":\"tool\",\"title\":\"允许执行命令\"}}\n\n"
      + "event: session.event\n"
      + "data: {\"event_type\":\"run.interrupted\",\"seq_id\":2,\"invocation_id\":\"run-approval\"}\n\n",
    ));

    expect(await screen.findByRole("region", { name: "待处理确认" })).toHaveTextContent("允许执行命令");
    await waitFor(() => expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "消息" })).not.toBeDisabled();
    expect(showToast).not.toHaveBeenCalledWith("云端运行未完成", expect.anything(), "error");
  });

  it("treats Kernel and custom Runtime approval SSE as waiting instead of failure", async () => {
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-direct-approval", title: "直流审批" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response([
          "event: response.output_item.done\ndata: {\"type\":\"response.output_item.done\",\"response_id\":\"resp-approval\",\"item\":{\"id\":\"approval-kernel\",\"type\":\"mcp_approval_request\",\"name\":\"Filesystem\",\"arguments\":{\"path\":\"/tmp\"}}}",
          "event: response.incomplete\ndata: {\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\",\"incomplete_details\":{\"reason\":\"tool_approval\"}}}",
          "event: response.approval_request\ndata: {\"type\":\"response.approval_request\",\"run_id\":\"run-custom\",\"interaction_id\":\"approval-custom\",\"interaction_kind\":\"approval\",\"request\":{\"title\":\"允许自定义工具\"}}",
        ].join("\n\n"), { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("直流审批");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "触发两种审批");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("Filesystem")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "待处理确认" })).toHaveTextContent("允许自定义工具");
    await waitFor(() => expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument());
    expect(screen.queryByText(/云端流式响应失败/)).not.toBeInTheDocument();
    expect(showToast).not.toHaveBeenCalledWith(expect.anything(), expect.anything(), "error");
  });

  it("streams chat and Responses chunks directly and reuses the same session on the second turn", async () => {
    let directCalls = 0;
    const directBodies: unknown[] = [];
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-direct", title: "直流会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        directCalls += 1;
        directBodies.push(JSON.parse(String(init.body)));
        const body = directCalls === 1
          ? [
            'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"reasoning_content":"先分析","content":"第一轮回答","tool_calls":[{"index":0,"id":"call-1","function":{"name":"lookup","arguments":"{\\"q\\":\\"one\\"}"}},{"index":1,"id":"call-2","function":{"name":"fetch_detail","arguments":"{\\"id\\":\\"two\\"}"}}]},"finish_reason":null}]}',
            "data: [DONE]",
            "",
          ].join("\n\n")
          : [
            "event: response.reasoning_summary_text.delta\ndata: {\"type\":\"response.reasoning_summary_text.delta\",\"delta\":\"再分析\"}",
            "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"item_id\":\"answer-1\",\"delta\":\"旧答案\"}",
            "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"item_id\":\"answer-1\",\"delta\":\"第二轮回答\",\"replace\":true}",
            "event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}",
            "",
          ].join("\n\n");
        return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("直流会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "第一轮");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("第一轮回答")).toBeInTheDocument();
    expect(screen.getByText("先分析")).toBeInTheDocument();
    expect(screen.getByText("lookup")).toBeInTheDocument();
    expect(screen.getByText("fetch_detail")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息" })).not.toBeDisabled());

    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "第二轮");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("第二轮回答")).toBeInTheDocument();
    expect(screen.queryByText(/旧答案/)).not.toBeInTheDocument();
    expect(screen.getByText("再分析")).toBeInTheDocument();
    expect(screen.getByText("已思考")).toBeInTheDocument();
    expect(directCalls).toBe(2);
    expect(directBodies).toEqual([
      expect.objectContaining({ content: [{ type: "input_text", text: "第一轮" }] }),
      expect.objectContaining({ content: [{ type: "input_text", text: "第二轮" }] }),
    ]);
    expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-direct/messages/stream`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("preserves soft line breaks, paragraphs, lists and fenced code in cloud Markdown", async () => {
    const markdown = [
      "第一行",
      "第二行",
      "",
      "独立段落",
      "",
      "- 项目一",
      "- 项目二",
      "",
      "```ts",
      "const answer = 42;",
      "```",
    ].join("\n");

    apiFetch.mockImplementation(async (path: string) => {
      if (path === `${base}/sessions`) {
        return jsonResponse({ sessions: [{ session_id: "sess-md", title: "Markdown 会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path === `${base}/sessions/sess-md/messages`) {
        return jsonResponse({ messages: [{ message_id: "msg-md", role: "assistant", content: markdown }] });
      }
      if (path === `${base}/sessions/sess-md/events?limit=1000`) return jsonResponse({ events: [] });
      throw new Error(`unexpected request: ${path}`);
    });

    const { container } = render(
      <CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />,
    );

    await screen.findByText("独立段落");
    const markdownRoot = container.querySelector("article.assistant .message-content");
    expect(markdownRoot).not.toBeNull();
    const paragraphs = markdownRoot!.querySelectorAll("p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0]).toHaveTextContent(/第一行\s+第二行/);
    expect(paragraphs[0].querySelector("br")).not.toBeNull();
    expect(within(markdownRoot as HTMLElement).getAllByRole("listitem")).toHaveLength(2);
    expect(markdownRoot!.querySelector("pre > code.language-ts")).toHaveTextContent("const answer = 42;");
  });

  it("re-lists after DELETE and clears selection for the deleted cloud session", async () => {
    let sessions = [
      { session_id: "sess-delete", title: "待删除会话" },
      { session_id: "sess-keep", title: "保留会话" },
    ];
    let listCalls = 0;

    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        listCalls += 1;
        return jsonResponse({ sessions });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path === `${base}/sessions/sess-delete` && init?.method === "DELETE") {
        sessions = sessions.filter(session => session.session_id !== "sess-delete");
        return new Response(null, { status: 204 });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    const { container } = render(
      <CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />,
    );

    await screen.findByText("待删除会话");
    await userEvent.click(screen.getByRole("button", { name: "删除会话 待删除会话" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-delete`,
      { method: "DELETE" },
    ));
    await waitFor(() => expect(listCalls).toBeGreaterThanOrEqual(2));
    expect(screen.queryByText("待删除会话")).not.toBeInTheDocument();
    expect(screen.getByText("保留会话")).toBeInTheDocument();
    expect(container.querySelector(".chat-session-item.active")).toBeNull();
    expect(screen.getByRole("heading", { name: "开始一段云端会话" })).toBeInTheDocument();
  });

  it("stops immediately on RunAgent 500 while preserving the user message and real error", async () => {
    const stream = new ReadableStream<Uint8Array>({ start() {} });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-failed-post", title: "失败会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return jsonResponse(
          { error: { message: "runtime admission rejected: provider unavailable" } },
          { status: 500 },
        );
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("失败会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "保留这条用户消息");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument());
    expect(screen.getByText("保留这条用户消息", { selector: ".message-content p" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
    expect(screen.getByText(/runtime admission rejected: provider unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试这条消息" })).toBeInTheDocument();
    expect(showToast).toHaveBeenCalledWith(
      "云端消息发送失败",
      "runtime admission rejected: provider unavailable",
      "error",
    );
  });

  it("does not repeatedly reload the complete cloud transcript while idle", async () => {
    let listCalls = 0;
    let messageCalls = 0;
    let eventCalls = 0;
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        listCalls += 1;
        return jsonResponse({ sessions: [{
          session_id: "sess-idle",
          title: "空闲会话",
          active_run_status: "completed",
        }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) {
        messageCalls += 1;
        return jsonResponse({ messages: [] });
      }
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        eventCalls += 1;
        return jsonResponse({ events: [] });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("空闲会话");
    await waitFor(() => expect(eventCalls).toBe(1));
    await new Promise(resolve => setTimeout(resolve, 4200));
    expect(listCalls).toBe(1);
    expect(messageCalls).toBe(1);
    expect(eventCalls).toBe(1);
  });

  it("rebuilds a stale message projection from canonical RuntimeEvents", async () => {
    const item = (
      seq: number,
      kind: "userMessage" | "agentMessage",
      itemId: string,
      text: string,
    ) => ({
      seq_id: seq,
      event_type: "runtime_event",
      invocation_id: "run-canonical-history",
      content: { runtime_event: {
        schema_version: 2,
        family: "runtime",
        event_id: `event-${seq}`,
        event_type: "item.completed",
        run_id: "run-canonical-history",
        scope_id: "scope-canonical-history",
        item_id: itemId,
        item_kind: kind === "agentMessage" ? "message" : "data",
        source: { framework: "codex", metadata: { native_item_kind: kind } },
        snapshot: { parts: [{ part_id: `${itemId}-part`, text, data: { type: kind } }] },
      } },
    });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-canonical-history", title: "Canonical 历史" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [{
        message_id: "stale-assistant",
        role: "assistant",
        content: "过时的消息投影",
        invocation_id: "run-canonical-history",
      }] });
      if (path.endsWith("/events?limit=1000") && !init?.method) return jsonResponse({
        events: [
          // Identity, not role + text, is the deduplication contract. Two
          // identical user items are distinct turns and must both survive a
          // cloud-history reconstruction.
          item(1, "userMessage", "user-1", "重复但独立的用户输入"),
          item(2, "userMessage", "user-2", "重复但独立的用户输入"),
          item(3, "agentMessage", "assistant-1", "Canonical 最终回答"),
        ],
      });
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    expect(await screen.findAllByText("重复但独立的用户输入")).toHaveLength(2);
    expect(screen.getByText("Canonical 最终回答")).toBeInTheDocument();
    expect(screen.queryByText("过时的消息投影")).not.toBeInTheDocument();
  });

  it("correlates the first server-owned invocation terminal while the direct stream is active", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { streamController = controller; },
    });
    const directStream = new ReadableStream<Uint8Array>({ start() {} });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-invocation", title: "Invocation 会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events?limit=1000") && !init?.method) {
        return jsonResponse({ events: [{ event_type: "user_message", seq_id: 5 }] });
      }
      if (path.endsWith("/events/stream?afterSeqId=5")) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("Invocation 会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "按 invocation 关联");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-invocation/messages/stream`,
      expect.objectContaining({ method: "POST" }),
    ));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-invocation/events/stream?afterSeqId=5`,
      expect.anything(),
    ));
    streamController?.enqueue(new TextEncoder().encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"run_status\",\"invocation_id\":\"run-server-owned\","
      + "\"content\":{\"status\":\"failed\",\"error\":\"runtime process crashed\"}}\n\n",
    ));

    expect(await screen.findByText(/runtime process crashed/)).toBeInTheDocument();
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
  });
});
