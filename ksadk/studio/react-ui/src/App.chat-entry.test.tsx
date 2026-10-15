import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import App from "./App";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
vi.mock("./useStudioViewportMode", () => ({ useStudioViewportMode: () => "desktop" }));
vi.mock("./useStudioTheme", () => ({
  useStudioTheme: () => ({ preference: "light", resolvedTheme: "light", setPreference: vi.fn() }),
}));
const chatEvents = vi.hoisted(() => ({ current: undefined as {
  agentId: string; requestedSessionId?: string;
  onSessionChanged?: (sessionId: string, agentId?: string, reason?: "cleanup") => void;
  onSelectConversation?: (sessionId?: string) => void;
} | undefined }));
vi.mock("./components/ChatWorkspace", () => ({
  ChatWorkspace: ({ agentId, agentName, credentialScope, requestedSessionId, onSessionChanged, onSelectConversation }: {
    agentId: string; agentName: string; credentialScope?: string; requestedSessionId?: string;
    onSessionChanged?: (sessionId: string, agentId?: string, reason?: "cleanup") => void;
    onSelectConversation?: (sessionId?: string) => void;
  }) => {
    chatEvents.current = { agentId, requestedSessionId, onSessionChanged, onSelectConversation };
    return (
    <div data-testid={agentId.startsWith("ar-") ? "cloud-chat-workspace" : "local-chat-workspace"} data-scope={credentialScope || ""}>
      {agentName} · {agentId}
    </div>
    );
  },
}));
vi.mock("./pages/CreatePage", () => ({
  CreatePage: ({ onCreated, quickCreateRequest }: { quickCreateRequest?: number; onCreated: (id?: string, openChat?: boolean) => void }) => (
    <button data-quick-create={quickCreateRequest || 0} type="button" onClick={() => onCreated("local-created", true)}>完成创建</button>
  ),
}));

const mockedFetch = vi.mocked(apiFetch);

function response(payload: unknown, ok = true): Response {
  return { ok, json: async () => payload } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(next => { resolve = next; });
  return { promise, resolve };
}

describe("Studio chat entry", () => {
  beforeEach(() => {
    chatEvents.current = undefined;
    window.history.replaceState(null, "", "#/conversations");
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
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") return response({ items: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") {
        return response({
          items: [{
            agentId: "ar-cloud-chat",
            name: "云端客服 Agent",
            status: "RUNNING",
            framework: "langgraph",
          }],
        });
      }
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
  });

  it("opens an account cloud Agent when no local Agent exists", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("cloud-chat-workspace")).toHaveTextContent(
        "云端客服 Agent · ar-cloud-chat",
      );
    });
    expect(screen.queryByText("先创建 Agent 才能开始会话")).not.toBeInTheDocument();
  });

  it.each([["创建 Agent", "0"], ["快速创建", "1"]])("uses only the requested creation mode for %s", async (label, request) => {
    window.history.replaceState(null, "", "#/agents");
    render(<App />);
    const button = await screen.findByRole("button", { name: label });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "完成创建" })).toHaveAttribute("data-quick-create", request);
  });

  it("passes the anonymous credential scope into the local conversation store", async () => {
    window.localStorage.setItem("agentkit-studio:chat-target:v1", "local:local-scoped");
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-scoped", name: "隔离 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-scoped") return response({ builds: [] });
      if (path === "/api/v1/deployments" || path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        return response({
          workspace: { name: "studio-test", path: "/workspace", workspaceId: "workspace-1" },
          operationScope: { workspace: "workspace-scope", cloudCredential: "credential-scope" },
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);

    await waitFor(() => expect(screen.getByTestId("local-chat-workspace")).toHaveAttribute("data-scope", "credential-scope"));
  });

  it("mirrors a selected local session into the reload URL without opening it again", async () => {
    window.localStorage.setItem("agentkit-studio:chat-target:v1", "local:local-scoped");
    const base = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => {
      if (String(input) === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-scoped", name: "隔离 Agent" } }] });
      }
      if (String(input) === "/api/v1/agents/local-scoped") return response({ builds: [] });
      return base(input, init);
    });
    render(<App />);
    await screen.findByTestId("local-chat-workspace");
    act(() => chatEvents.current?.onSessionChanged?.("session /1", "local-scoped"));
    const route = new URLSearchParams(window.location.hash.split("?")[1]);
    expect(route.get("agentId")).toBe("local-scoped");
    expect(route.get("sessionId")).toBe("session /1");
    expect(chatEvents.current?.requestedSessionId).toBe("");
    act(() => chatEvents.current?.onSelectConversation?.("session /1"));
    expect(new URLSearchParams(window.location.hash.split("?")[1]).get("sessionId")).toBe("session /1");
    const selectedHash = window.location.hash;
    act(() => chatEvents.current?.onSessionChanged?.("foreign-session", "other-agent"));
    act(() => chatEvents.current?.onSessionChanged?.("", "local-scoped", "cleanup"));
    expect(window.location.hash).toBe(selectedHash);
    act(() => {
      window.history.pushState(null, "", "#/agents");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    act(() => chatEvents.current?.onSessionChanged?.("background-session", "local-scoped"));
    expect(window.location.hash).toBe("#/agents");
  });

  it("keeps a requested deep link while an older local session is still restoring", async () => {
    window.history.replaceState(null, "", "#/conversations?agentId=local-scoped&sessionId=requested-session");
    const base = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => {
      if (String(input) === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-scoped", name: "隔离 Agent" } }] });
      }
      if (String(input) === "/api/v1/agents/local-scoped") return response({ builds: [] });
      return base(input, init);
    });
    render(<App />);
    await screen.findByTestId("local-chat-workspace");
    act(() => chatEvents.current?.onSessionChanged?.("older-session", "local-scoped"));
    expect(new URLSearchParams(window.location.hash.split("?")[1]).get("sessionId")).toBe("requested-session");
    expect(chatEvents.current?.requestedSessionId).toBe("requested-session");
    act(() => chatEvents.current?.onSessionChanged?.("requested-session", "local-scoped"));
    act(() => chatEvents.current?.onSessionChanged?.("", "local-scoped"));
    expect(new URLSearchParams(window.location.hash.split("?")[1]).get("sessionId")).toBeNull();
    expect(chatEvents.current?.requestedSessionId).toBe("");
  });

  it("waits for the workspace identity before mounting a restored conversation", async () => {
    window.localStorage.setItem("agentkit-studio:chat-target:v1", "local:local-scoped");
    const bootstrap = deferred<Response>();
    const base = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/v1/system/bootstrap") return bootstrap.promise;
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-scoped", name: "隔离 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-scoped") return response({ builds: [] });
      return base(input, init);
    });

    await act(async () => { render(<App />); });
    expect(screen.queryByTestId("local-chat-workspace")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "正在连接工作区" })).toBeVisible();

    bootstrap.resolve(response({
      workspace: { name: "studio-test", path: "/workspace", workspaceId: "workspace-1" },
      operationScope: { workspace: "workspace-scope", cloudCredential: "credential-scope" },
    }));
    await waitFor(() => expect(screen.getByTestId("local-chat-workspace")).toHaveAttribute("data-scope", "credential-scope"));
  });

  it.each(["network", "http"])("preserves the conversation owner after a %s bootstrap refresh failure", async failure => {
    window.localStorage.setItem("agentkit-studio:chat-target:v1", "local:local-scoped");
    const base = mockedFetch.getMockImplementation()!;
    let bootstrapReads = 0;
    mockedFetch.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/v1/system/bootstrap") {
        bootstrapReads += 1;
        if (bootstrapReads > 1) {
          if (failure === "network") throw new Error("fixture bootstrap unavailable");
          return response({ message: "fixture bootstrap unavailable" }, false);
        }
        return response({
          workspace: { name: "studio-test", path: "/workspace", workspaceId: "workspace-1" },
          operationScope: { workspace: "workspace-scope", cloudCredential: "credential-scope" },
        });
      }
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-scoped", name: "隔离 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-scoped") return response({ builds: [] });
      return base(input, init);
    });

    render(<App />);
    const conversation = await screen.findByTestId("local-chat-workspace");
    expect(conversation).toHaveAttribute("data-scope", "credential-scope");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "刷新" })); });
    expect(bootstrapReads).toBe(2);
    expect(screen.getByTestId("local-chat-workspace")).toBe(conversation);
    expect(conversation).toHaveAttribute("data-scope", "credential-scope");
  });

  it("restores the selected cloud Agent after a full page reload", async () => {
    window.localStorage.setItem(
      "agentkit-studio:chat-target:v1",
      "cloud:account:ar-cloud-chat",
    );
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-1", name: "本地 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-1") return response({ builds: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") {
        return response({
          items: [{
            agentId: "ar-cloud-chat",
            name: "云端客服 Agent",
            status: "RUNNING",
            framework: "langgraph",
          }],
        });
      }
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("cloud-chat-workspace")).toHaveTextContent(
        "云端客服 Agent · ar-cloud-chat",
      );
    });
    expect(screen.queryByTestId("local-chat-workspace")).not.toBeInTheDocument();
  });

  it("restores the selected local Agent after a full page reload", async () => {
    window.localStorage.setItem(
      "agentkit-studio:chat-target:v1",
      "local:local-2",
    );
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [
          { metadata: { id: "local-1", name: "第一个 Agent" } },
          { metadata: { id: "local-2", name: "已选 Agent" } },
        ] });
      }
      if (path === "/api/v1/agents/local-1" || path === "/api/v1/agents/local-2") {
        return response({ builds: [] });
      }
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("local-chat-workspace")).toHaveTextContent(
        "已选 Agent · local-2",
      );
    });
  });

  it("keeps an explicitly created local Agent selected while its directory refreshes", async () => {
    window.history.replaceState(null, "", "#/create");
    window.localStorage.setItem(
      "agentkit-studio:chat-target:v1",
      "cloud:account:ar-cloud-chat",
    );
    let agentListReads = 0;
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        agentListReads += 1;
        return response({
          items: agentListReads === 1
            ? []
            : [{ metadata: { id: "local-created", name: "新建本地 Agent" } }],
        });
      }
      if (path === "/api/v1/agents/local-created") return response({ builds: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") {
        return response({
          items: [{
            agentId: "ar-cloud-chat",
            name: "云端客服 Agent",
            status: "RUNNING",
            framework: "langgraph",
          }],
        });
      }
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    await screen.getByRole("button", { name: "完成创建" }).click();

    await waitFor(() => {
      expect(screen.getByTestId("local-chat-workspace")).toHaveTextContent("新建本地 Agent");
    });
    expect(screen.queryByTestId("cloud-chat-workspace")).not.toBeInTheDocument();
  });

  it("ignores a stale local Agent discovery response after a newer refresh", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    let agentListReads = 0;
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        agentListReads += 1;
        return (agentListReads === 1 ? first.promise : second.promise);
      }
      if (path === "/api/v1/agents/new-agent" || path === "/api/v1/agents/old-agent") {
        return response({ builds: [] });
      }
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    await waitFor(() => expect(agentListReads).toBe(1));
    await screen.getByRole("button", { name: "刷新" }).click();
    await waitFor(() => expect(agentListReads).toBe(2));

    second.resolve(response({ items: [{ metadata: { id: "new-agent", name: "最新 Agent" } }] }));
    await waitFor(() => expect(screen.getByTestId("local-chat-workspace")).toHaveTextContent("最新 Agent"));

    first.resolve(response({ items: [{ metadata: { id: "old-agent", name: "过期 Agent" } }] }));
    await waitFor(() => expect(screen.getByTestId("local-chat-workspace")).toHaveTextContent("最新 Agent"));
    expect(screen.queryByText("过期 Agent")).not.toBeInTheDocument();
  });

  it("ignores a stale workspace bootstrap response after a newer refresh", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    let bootstrapReads = 0;
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") return response({ items: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        bootstrapReads += 1;
        return (bootstrapReads === 1 ? first.promise : second.promise);
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    await waitFor(() => expect(bootstrapReads).toBe(1));
    await screen.getByRole("button", { name: "刷新" }).click();
    await waitFor(() => expect(bootstrapReads).toBe(2));

    second.resolve(response({ workspace: { name: "最新工作区", path: "/new-workspace" } }));
    await waitFor(() => expect(screen.getByRole("button", { name: /最新工作区/ })).toBeInTheDocument());

    first.resolve(response({ workspace: { name: "过期工作区", path: "/old-workspace" } }));
    await waitFor(() => expect(screen.getByRole("button", { name: /最新工作区/ })).toBeInTheDocument());
    expect(screen.queryByText("过期工作区")).not.toBeInTheDocument();
  });

  it("keeps the local conversation usable while cloud discovery is unavailable", async () => {
    const deployments = deferred<Response>();
    const cloudAgents = deferred<Response>();
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-online", name: "本地在线 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-online") return response({ builds: [] });
      if (path === "/api/v1/deployments") return deployments.promise;
      if (path === "/api/v1/cloud-agents?size=100") return cloudAgents.promise;
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("local-chat-workspace")).toHaveTextContent("本地在线 Agent");
    });
    expect(screen.queryByTestId("cloud-chat-workspace")).not.toBeInTheDocument();

    deployments.resolve(response({ items: [] }));
    cloudAgents.resolve(response({ items: [] }));
  });

  it("opens the Agent target switcher with Cmd/Ctrl+K", async () => {
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-1", name: "目标 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-1") return response({ builds: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    const selector = await screen.findByRole("combobox", { name: "切换会话目标" });
    const event = new KeyboardEvent("keydown", { key: "k", metaKey: true, cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(selector).toHaveAttribute("id", "conversation-target-selector");
    expect(document.activeElement).toBe(selector);
    expect(await screen.findByRole("option", { name: "本地 · 目标 Agent" })).toBeInTheDocument();
  });

  it("focuses the composer after creating a draft with Cmd/Ctrl+N", async () => {
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-1", name: "目标 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-1") return response({ builds: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    const composer = document.createElement("textarea");
    const form = document.createElement("form");
    form.dataset.ui = "sender";
    form.append(composer);
    document.body.append(form);
    const event = new KeyboardEvent("keydown", { key: "n", metaKey: true, cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => expect(document.activeElement).toBe(composer));
    form.remove();
  });

  it("opens current conversation search with Cmd/Ctrl+F", async () => {
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=100") {
        return response({ items: [{ metadata: { id: "local-1", name: "目标 Agent" } }] });
      }
      if (path === "/api/v1/agents/local-1") return response({ builds: [] });
      if (path === "/api/v1/deployments") return response({ items: [] });
      if (path === "/api/v1/cloud-agents?size=100") return response({ items: [] });
      if (path === "/api/v1/system/bootstrap") {
        return response({ workspace: { name: "studio-test", path: "/workspace" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    const find = document.createElement("button");
    find.type = "button";
    find.setAttribute("aria-label", "查找当前会话");
    document.body.append(find);
    const clicked = vi.fn();
    find.addEventListener("click", clicked);
    const event = new KeyboardEvent("keydown", { key: "f", ctrlKey: true, cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(find);
    expect(clicked).toHaveBeenCalledTimes(1);
    find.remove();
  });
});
