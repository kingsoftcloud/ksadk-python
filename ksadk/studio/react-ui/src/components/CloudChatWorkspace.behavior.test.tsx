import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("../api", () => ({ apiFetch }));

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
});
