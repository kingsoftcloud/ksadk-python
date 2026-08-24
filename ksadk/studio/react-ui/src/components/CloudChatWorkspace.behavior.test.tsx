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
      if (path === `${base}/sessions/sess-1/events` && !init?.method) {
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

    expect(await screen.findByText(/第一段\s+第二段/)).toBeInTheDocument();
    expect(screen.getAllByText(/第一段/)).toHaveLength(1);
    expect(screen.getByText(/正在等待云端响应/)).toBeInTheDocument();
    directStreamController?.enqueue(encoder.encode("data: [DONE]\n\n"));
    await waitFor(() => expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument());
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
      if (path.endsWith("/events") && !init?.method) {
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
        content: { runtime_event: { ...runtimeEvent, run_id: "run-qwen", scope_id: "scope-qwen" } },
      })}\n\n`
    )).join("");
    directStreamController?.enqueue(new TextEncoder().encode(encoded));

    expect(await screen.findByText("实时回答第一段")).toBeInTheDocument();
    expect(screen.getByText(/正在分析问题/)).toBeInTheDocument();
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "待处理确认" })).toHaveTextContent("允许查询天气");
    expect(screen.getByText(/正在等待云端响应/)).toBeInTheDocument();
    directStreamController?.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
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
      if (path.endsWith("/events") && !init?.method) return jsonResponse({ events: [] });
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

    eventStreamController?.enqueue(new TextEncoder().encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"interaction.requested\",\"seq_id\":1,\"invocation_id\":\"inv-approval\","
      + "\"interaction_id\":\"approval-1\",\"interaction_kind\":\"approval\","
      + "\"request\":{\"kind\":\"tool\",\"title\":\"允许执行命令\"}}\n\n"
      + "event: session.event\n"
      + "data: {\"event_type\":\"run.interrupted\",\"seq_id\":2,\"invocation_id\":\"inv-approval\"}\n\n",
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
      if (path.endsWith("/events") && !init?.method) return jsonResponse({ events: [] });
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
      if (path.endsWith("/events") && !init?.method) return jsonResponse({ events: [] });
      if (path.endsWith("/events/stream?afterSeqId=0")) {
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        directCalls += 1;
        directBodies.push(JSON.parse(String(init.body)));
        const body = directCalls === 1
          ? [
            'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"reasoning_content":"先分析","content":"第一轮回答","tool_calls":[{"index":0,"id":"call-1","function":{"name":"lookup","arguments":"{\\"q\\":\\"one\\"}"}}]},"finish_reason":null}]}',
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
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息" })).not.toBeDisabled());

    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "第二轮");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("第二轮回答")).toBeInTheDocument();
    expect(screen.queryByText(/旧答案/)).not.toBeInTheDocument();
    expect(screen.getByText("再分析")).toBeInTheDocument();
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

  it("stops polling when the selected session projects active_run_status failed", async () => {
    let listCalls = 0;
    const eventStream = new ReadableStream<Uint8Array>({ start() {} });
    const directStream = new ReadableStream<Uint8Array>({ start() {} });
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
        return new Response(eventStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (path.endsWith("/messages/stream") && init?.method === "POST") {
        return new Response(directStream, { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CloudChatWorkspace deploymentId="dep-cloud" agentId="ar-cloud" agentName="Cloud Agent" />);

    await screen.findByText("状态失败会话");
    await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "触发失败状态");
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText(/worker exited before producing a reply/, {}, { timeout: 2500 })).toBeInTheDocument();
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
    expect(showToast).toHaveBeenCalledWith(
      "云端运行未完成",
      "worker exited before producing a reply",
      "error",
    );
  });

  it("correlates an invocation_id terminal SessionEvent while the direct stream is active", async () => {
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
      if (path.endsWith("/events") && !init?.method) {
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
    streamController?.enqueue(new TextEncoder().encode(
      "event: session.event\n"
      + "data: {\"event_type\":\"run_status\",\"invocation_id\":\"inv-500\","
      + "\"content\":{\"status\":\"failed\",\"error\":\"runtime process crashed\"}}\n\n",
    ));

    expect(await screen.findByText(/runtime process crashed/)).toBeInTheDocument();
    expect(screen.queryByText(/正在等待云端响应/)).not.toBeInTheDocument();
  });
});
