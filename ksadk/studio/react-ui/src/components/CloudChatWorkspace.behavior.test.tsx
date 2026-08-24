import { render, screen, waitFor, within } from "@testing-library/react";
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
      if (path.endsWith("/events")) return jsonResponse({ events: [] });
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
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    let resolvePost: ((response: Response) => void) | undefined;
    const postResponse = new Promise<Response>(resolve => { resolvePost = resolve; });
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });

    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-1", title: "流式会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path === `${base}/sessions/sess-1/messages` && !init?.method) {
        return jsonResponse({ messages: [] });
      }
      if (path === `${base}/sessions/sess-1/events` && !init?.method) {
        return jsonResponse({ events: [{ event_type: "run.completed", seq_id: 5 }] });
      }
      if (path === `${base}/sessions/sess-1/events/stream?afterSeqId=5`) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path === `${base}/sessions/sess-1/messages` && init?.method === "POST") {
        return postResponse;
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

    streamController?.enqueue(encoder.encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"item.updated\",\"seq_id\":6,\"run_id\":\"run-1\","
      + "\"content\":{\"runtime_event\":{\"item_kind\":\"message\",\"op\":\"append\","
      + "\"update\":{\"text\":\"第一段\"}}}}\n\n"
      + "event: session.event\n"
      + "data: {\"event_type\":\"item.updated\",\"seq_id\":7,\"run_id\":\"run-1\","
      + "\"content\":{\"runtime_event\":{\"item_kind\":\"message\",\"op\":\"append\","
      + "\"update\":{\"text\":\"\\n第二段\"}}}}\n\n",
    ));

    expect(await screen.findByText(/第一段\s+第二段/)).toBeInTheDocument();
    expect(screen.getByText(/正在等待云端响应/)).toBeInTheDocument();
    resolvePost?.(jsonResponse(
      { receipt_status: "accepted", run_id: "run-1", accepted_seq: 5 },
      { status: 202 },
    ));
  });

  it("projects canonical nested RuntimeEvent items before RunAgent returns", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    let resolvePost: ((response: Response) => void) | undefined;
    const postResponse = new Promise<Response>(resolve => { resolvePost = resolve; });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { streamController = controller; },
    });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-canonical", title: "Canonical 会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events") && !init?.method) {
        return jsonResponse({ events: [{ event_type: "user_message", seq_id: 10 }] });
      }
      if (path.endsWith("/events/stream?afterSeqId=10")) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages") && init?.method === "POST") return postResponse;
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
        content: { runtime_event: { ...runtimeEvent, run_id: "run-qwen", scope_id: "scope-qwen" } },
      })}\n\n`
    )).join("");
    streamController?.enqueue(new TextEncoder().encode(encoded));

    expect(await screen.findByText("实时回答第一段")).toBeInTheDocument();
    expect(screen.getByText(/正在分析问题/)).toBeInTheDocument();
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "待处理确认" })).toHaveTextContent("允许查询天气");
    expect(screen.getByText(/正在等待云端响应/)).toBeInTheDocument();
    resolvePost?.(jsonResponse({
      receipt_status: "accepted",
      run_id: "run-qwen",
      invocation_id: "inv-qwen",
      accepted_seq: 10,
    }, { status: 202 }));
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
      if (path === `${base}/sessions/sess-md/events`) return jsonResponse({ events: [] });
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
      if (path.endsWith("/events") && !init?.method) return jsonResponse({ events: [] });
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
      if (path.endsWith("/events") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages") && init?.method === "POST") {
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

  it("stops polling when the selected session projects active_run_status failed", async () => {
    let listCalls = 0;
    const stream = new ReadableStream<Uint8Array>({ start() {} });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        listCalls += 1;
        return jsonResponse({ sessions: [{
          session_id: "sess-session-failed",
          title: "状态失败会话",
          active_run_status: listCalls === 1 ? "running" : "failed",
          active_run_error: "worker exited before producing a reply",
        }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages") && init?.method === "POST") {
        return jsonResponse({ receipt_status: "accepted", invocation_id: "inv-failed" }, { status: 202 });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("状态失败会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "触发失败状态");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText(/worker exited before producing a reply/)).toBeInTheDocument();
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
    expect(showToast).toHaveBeenCalledWith(
      "云端运行未完成",
      "worker exited before producing a reply",
      "error",
    );
  });

  it("correlates an invocation_id receipt with an invocation_id terminal SSE frame", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { streamController = controller; },
    });
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `${base}/sessions` && !init?.method) {
        return jsonResponse({ sessions: [{ session_id: "sess-invocation", title: "Invocation 会话" }] });
      }
      if (path === `${base}/models`) return jsonResponse({ models: [] });
      if (path.endsWith("/messages") && !init?.method) return jsonResponse({ messages: [] });
      if (path.endsWith("/events") && !init?.method) {
        return jsonResponse({ events: [{ event_type: "user_message", seq_id: 5 }] });
      }
      if (path.endsWith("/events/stream?afterSeqId=5")) {
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages") && init?.method === "POST") {
        return jsonResponse({
          receipt_status: "accepted",
          run_id: "run-500",
          invocation_id: "inv-500",
        }, { status: 202 });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("Invocation 会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "按 invocation 关联");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      `${base}/sessions/sess-invocation/messages`,
      expect.objectContaining({ method: "POST" }),
    ));
    streamController?.enqueue(new TextEncoder().encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"run_status\",\"invocation_id\":\"inv-500\","
      + "\"content\":{\"status\":\"failed\",\"error\":\"runtime process crashed\"}}\n\n",
    ));

    expect(await screen.findByText(/runtime process crashed/)).toBeInTheDocument();
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
  });
});
