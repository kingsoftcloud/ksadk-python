import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch, showToast } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("../api", () => ({ apiFetch }));
vi.mock("./Toast", () => ({ showToast }));

import { ChatWorkspace } from "./ChatWorkspace";

function jsonResponse(value: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ChatWorkspace ConversationSurface behavior", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it("fetches the agent-scoped surface and hides undeclared composer inputs", async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === "/api/v1/runs") return jsonResponse({ items: [] });
      if (path === "/api/v1/agents/surface-agent/models") {
        return jsonResponse({
          Current: "qwen3.7-flash",
          Models: [{
            id: "qwen3.7-flash",
            display_name: "qwen3.7-flash",
            capabilities: { reasoning_efforts: ["low", "high"] },
          }],
        });
      }
      if (path.startsWith("/api/v1/agents/surface-agent/conversation-surface?sessionId=")) {
        const sessionId = decodeURIComponent(path.split("sessionId=")[1]);
        return jsonResponse({
          buildId: "build-surface",
          surface: {
            apiVersion: "conversation.ksadk.io/v1",
            kind: "ConversationSurface",
            surfaceId: "studio.build.build-surface",
            sessionId,
            providerRef: "studio.runtime.langgraph",
            inputs: [{ name: "text", mode: "native" }],
            outputs: [{ name: "streaming", mode: "translated" }],
          },
        });
      }
      if (path === "/api/v1/builds/build-surface/conversation:stream") {
        return new Response(
          [
            'id: 1\nevent: message.delta\ndata: {"conversationItem":{"apiVersion":"conversation.ksadk.io/v1","kindVersion":1,"itemId":"answer-1","sourceEventIds":["event-1"],"sessionId":"session","runId":"run-1","kind":"assistant_text","operation":"append","lifecycle":"streaming","visibility":"public","payloadSchemaRef":"conversation.item.assistant_text/v1","payload":{"text":"完成"},"nativeRef":{}}}\n\n',
            'id: 2\nevent: run.completed\ndata: {"conversationItem":{"apiVersion":"conversation.ksadk.io/v1","kindVersion":1,"itemId":"run-end","sourceEventIds":["event-2"],"sessionId":"session","runId":"run-1","kind":"progress","operation":"completed","lifecycle":"completed","visibility":"public","payloadSchemaRef":"conversation.item.progress/v1","payload":{},"nativeRef":{}}}\n\n',
          ].join(""),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      throw new Error(`unexpected request: ${path}`);
    });

    const user = userEvent.setup();
    render(<ChatWorkspace agentId="surface-agent" agentName="Surface Agent" />);

    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息" })).toBeEnabled());
    expect(apiFetch).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/v1\/agents\/surface-agent\/conversation-surface\?sessionId=/),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.queryByRole("button", { name: "添加附件或运行控制" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批准模式/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /模型|推理强度/ })).not.toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "消息" }), "只发送文字");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/builds/build-surface/conversation:stream",
      expect.objectContaining({ method: "POST" }),
    ));
    const responseCall = apiFetch.mock.calls.find(([path]) => path === "/api/v1/builds/build-surface/conversation:stream");
    const body = JSON.parse(String(responseCall?.[1]?.body));
    expect(body.input.parts).toEqual([{ kind: "text", text: "只发送文字" }]);
    expect(body.input).not.toHaveProperty("modelRef");
    expect(body.input).not.toHaveProperty("reasoning");
    expect(body.input).not.toHaveProperty("approvalMode");
    expect(body.input).not.toHaveProperty("collaborationMode");
    expect(body.input).not.toHaveProperty("goalObjective");
    expect(responseCall?.[1]?.headers).toMatchObject({
      "Content-Type": "application/json",
      "Idempotency-Key": body.input.idempotencyKey,
    });
    expect(apiFetch.mock.calls.some(([path]) => path === "/v1/responses")).toBe(false);
  });

  it("keeps the legacy composer when an older Studio has no surface endpoint", async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === "/api/v1/runs") return jsonResponse({ items: [] });
      if (path === "/api/v1/agents/legacy-agent/models") {
        return jsonResponse({
          Current: "qwen3.7-flash",
          Models: [{ id: "qwen3.7-flash", display_name: "qwen3.7-flash" }],
        });
      }
      if (path.startsWith("/api/v1/agents/legacy-agent/conversation-surface?sessionId=")) {
        return jsonResponse({ error: { message: "not found" } }, { status: 404 });
      }
      if (path === "/v1/responses") {
        return new Response(
          'event: response.completed\ndata: {"type":"response.completed","response":{"id":"legacy-response","status":"completed","output":[]}}\n\n',
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<ChatWorkspace agentId="legacy-agent" agentName="Legacy Agent" />);

    expect(await screen.findByRole("button", { name: "添加附件或运行控制" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批准模式：帮我批准" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "模型 qwen3.7-flash" })).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息" }), "旧 Agent 继续工作");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/v1/responses",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("reconnects only the typed conversation transport from its durable event cursor", async () => {
    const item = (sourceEventId: string, text: string, lifecycle = "streaming", operation = "append") => ({
      apiVersion: "conversation.ksadk.io/v1",
      kindVersion: 1,
      itemId: "answer-1",
      sourceEventIds: [sourceEventId],
      sessionId: "session-reconnect",
      runId: "run-reconnect",
      kind: "assistant_text",
      operation,
      lifecycle,
      visibility: "public",
      payloadSchemaRef: "conversation.item.assistant_text/v1",
      payload: { text },
      nativeRef: {},
    });
    const frame = (id: number, type: string, payload: unknown) => (
      `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`
    );
    apiFetch.mockImplementation(async (path: string) => {
      if (path === "/api/v1/runs") return jsonResponse({ items: [] });
      if (path === "/api/v1/agents/reconnect-agent/models") {
        return jsonResponse({
          Current: "qwen3.7-flash",
          Models: [{ id: "qwen3.7-flash", display_name: "qwen3.7-flash" }],
        });
      }
      if (path.startsWith("/api/v1/agents/reconnect-agent/conversation-surface?sessionId=")) {
        const sessionId = decodeURIComponent(path.split("sessionId=")[1]);
        return jsonResponse({
          buildId: "build-reconnect",
          surface: {
            apiVersion: "conversation.ksadk.io/v1",
            kind: "ConversationSurface",
            surfaceId: "studio.build.build-reconnect",
            sessionId,
            providerRef: "studio.runtime.langgraph",
            inputs: [{ name: "text", mode: "native" }],
            outputs: [{ name: "streaming", mode: "translated" }],
          },
        });
      }
      if (path === "/api/v1/builds/build-reconnect/conversation:stream") {
        return new Response(frame(1, "message.delta", { conversationItem: item("source-1", "part") }), {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (path === "/api/v1/runs/run-reconnect/events?after=1") {
        return new Response([
          frame(1, "message.delta", { conversationItem: item("source-1", "part") }),
          frame(2, "run.completed", {
            conversationItem: {
              ...item("source-2", "", "completed", "completed"),
              itemId: "run-end",
              kind: "progress",
              payloadSchemaRef: "conversation.item.progress/v1",
              payload: {},
            },
          }),
        ].join(""), { headers: { "Content-Type": "text/event-stream" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    const user = userEvent.setup();
    render(<ChatWorkspace agentId="reconnect-agent" agentName="Reconnect Agent" />);
    const textbox = await screen.findByRole("textbox", { name: "消息" });
    await user.type(textbox, "断线续流");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/runs/run-reconnect/events?after=1",
      expect.objectContaining({
        headers: { "Last-Event-ID": "1" },
        signal: expect.any(AbortSignal),
      }),
    ));
    expect(apiFetch.mock.calls.filter(([path]) => (
      path === "/api/v1/builds/build-reconnect/conversation:stream"
    ))).toHaveLength(1);
    expect(apiFetch.mock.calls.some(([path]) => path === "/v1/responses")).toBe(false);
  });

  it("does not guess a replay Run when the typed stream returned no canonical item", async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === "/api/v1/runs") return jsonResponse({ items: [] });
      if (path === "/api/v1/agents/no-item-agent/models") {
        return jsonResponse({
          Current: "qwen3.7-flash",
          Models: [{ id: "qwen3.7-flash", display_name: "qwen3.7-flash" }],
        });
      }
      if (path.startsWith("/api/v1/agents/no-item-agent/conversation-surface?sessionId=")) {
        const sessionId = decodeURIComponent(path.split("sessionId=")[1]);
        return jsonResponse({
          buildId: "build-no-item",
          surface: {
            apiVersion: "conversation.ksadk.io/v1",
            kind: "ConversationSurface",
            surfaceId: "studio.build.build-no-item",
            sessionId,
            providerRef: "studio.runtime.langgraph",
            inputs: [{ name: "text", mode: "native" }],
            outputs: [{ name: "streaming", mode: "translated" }],
          },
        });
      }
      if (path === "/api/v1/builds/build-no-item/conversation:stream") {
        return new Response(
          'id: 1\nevent: run.created\ndata: {"runId":"run-must-not-be-guessed"}\n\n',
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      throw new Error(`unexpected request: ${path}`);
    });

    const user = userEvent.setup();
    render(<ChatWorkspace agentId="no-item-agent" agentName="No Item Agent" />);
    const textbox = await screen.findByRole("textbox", { name: "消息" });
    await user.type(textbox, "不要猜 Run");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "运行失败",
      expect.stringContaining("可恢复的 Run 标识前中断"),
      "error",
    ));
    expect(apiFetch.mock.calls.some(([path]) => (
      String(path).startsWith("/api/v1/runs/run-must-not-be-guessed")
    ))).toBe(false);
  });

  it("fails closed when the current Studio cannot prove the active surface", async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === "/api/v1/runs") return jsonResponse({ items: [] });
      if (path === "/api/v1/agents/broken-agent/models") {
        return jsonResponse({
          Current: "qwen3.7-flash",
          Models: [{ id: "qwen3.7-flash", display_name: "qwen3.7-flash" }],
        });
      }
      if (path.startsWith("/api/v1/agents/broken-agent/conversation-surface?sessionId=")) {
        return jsonResponse({ error: { message: "build failed" } }, { status: 500 });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<ChatWorkspace agentId="broken-agent" agentName="Broken Agent" />);

    const input = await screen.findByRole("textbox", { name: "消息" });
    await waitFor(() => expect(input).toBeDisabled());
    expect(input).toHaveAttribute("placeholder", "会话能力加载失败，请刷新后重试");
    expect(screen.queryByRole("button", { name: "添加附件或运行控制" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批准模式/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /模型|推理强度/ })).not.toBeInTheDocument();
    expect(apiFetch.mock.calls.some(([path]) => path === "/v1/responses")).toBe(false);
  });

  it("fails closed on a network error instead of guessing legacy capabilities", async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === "/api/v1/runs") return jsonResponse({ items: [] });
      if (path === "/api/v1/agents/offline-agent/models") {
        return jsonResponse({
          Current: "qwen3.7-flash",
          Models: [{ id: "qwen3.7-flash", display_name: "qwen3.7-flash" }],
        });
      }
      if (path.startsWith("/api/v1/agents/offline-agent/conversation-surface?sessionId=")) {
        throw new TypeError("Failed to fetch");
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<ChatWorkspace agentId="offline-agent" agentName="Offline Agent" />);

    const input = await screen.findByRole("textbox", { name: "消息" });
    await waitFor(() => expect(input).toHaveAttribute(
      "placeholder",
      "会话能力加载失败，请刷新后重试",
    ));
    expect(input).toBeDisabled();
    expect(screen.queryByRole("button", { name: "添加附件或运行控制" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批准模式/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /模型|推理强度/ })).not.toBeInTheDocument();
    expect(apiFetch.mock.calls.some(([path]) => path === "/v1/responses")).toBe(false);
  });
});
