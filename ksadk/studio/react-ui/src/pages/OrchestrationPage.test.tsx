import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { OrchestrationPage } from "./OrchestrationPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

describe("OrchestrationPage graph canvas", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v1/catalog/resources")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/runs")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 2 },
            spec: {
              runtime: { type: "codex" },
              execution: { strategy: "direct" },
              bindings: {
                modelProfileId: "glm-5.2",
                tools: ["search"],
                mcpServers: ["metaso"],
                skills: [],
              },
            },
          },
        }),
      } as Response;
    });
  });

  it("renders the execution path in a compact clean canvas with fit controls", async () => {
    render(
      <OrchestrationPage
        currentAgentId="agentkit-a1b2c3d4"
        agents={[]}
        onSelectAgent={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    const canvas = await screen.findByRole("application", { name: "执行链路画布" });
    expect(canvas).toHaveAttribute("data-layout", "adaptive-serpentine");
    expect(canvas).toHaveAttribute("data-background", "plain");
    expect(screen.getByRole("button", { name: "适应画布" })).toBeVisible();
    expect(screen.getByText("任务输入")).toBeInTheDocument();
    expect(screen.getByText("2 个能力绑定")).toBeInTheDocument();
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(4));
  });
});
