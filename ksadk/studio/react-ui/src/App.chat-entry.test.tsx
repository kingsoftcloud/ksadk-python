import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import App from "./App";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
vi.mock("./useStudioViewportMode", () => ({ useStudioViewportMode: () => "desktop" }));
vi.mock("./useStudioTheme", () => ({
  useStudioTheme: () => ({ preference: "light", resolvedTheme: "light", setPreference: vi.fn() }),
}));
vi.mock("./components/CloudChatWorkspace", () => ({
  CloudChatWorkspace: ({ agentId, agentName }: { agentId: string; agentName: string }) => (
    <div data-testid="cloud-chat-workspace">{agentName} · {agentId}</div>
  ),
}));
vi.mock("./components/ChatWorkspace", () => ({
  ChatWorkspace: ({ agentName }: { agentName: string }) => (
    <div data-testid="local-chat-workspace">{agentName}</div>
  ),
}));

const mockedFetch = vi.mocked(apiFetch);

function response(payload: unknown, ok = true): Response {
  return { ok, json: async () => payload } as Response;
}

describe("Studio chat entry", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "#/conversations");
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
});
