import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import App from "./App";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
vi.mock("./useStudioViewportMode", () => ({ useStudioViewportMode: () => "desktop" }));
vi.mock("./useStudioTheme", () => ({
  useStudioTheme: () => ({ preference: "light", resolvedTheme: "light", setPreference: vi.fn() }),
}));
vi.mock("./components/ChatWorkspace", () => ({
  ChatWorkspace: ({ agentId, agentName }: { agentId: string; agentName: string }) => (
    <div data-testid={agentId.startsWith("ar-") ? "cloud-chat-workspace" : "local-chat-workspace"}>
      {agentName} · {agentId}
    </div>
  ),
}));
vi.mock("./pages/CreatePage", () => ({
  CreatePage: ({ onCreated }: { onCreated: (id?: string, openChat?: boolean) => void }) => (
    <button type="button" onClick={() => onCreated("local-created", true)}>完成创建</button>
  ),
}));

const mockedFetch = vi.mocked(apiFetch);

function response(payload: unknown, ok = true): Response {
  return { ok, json: async () => payload } as Response;
}

describe("Studio chat entry", () => {
  beforeEach(() => {
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
});
