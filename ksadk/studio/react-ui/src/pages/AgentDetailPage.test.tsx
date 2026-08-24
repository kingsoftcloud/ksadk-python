import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
let deploymentItems: unknown[] = [];

apiFetch.mockImplementation(async (path: string) => {
  if (path.startsWith("/api/v1/agents/")) {
    return new Response(JSON.stringify({
      draft: {
        metadata: { id: "demo-agent", name: "Demo Agent", revision: 1 },
        spec: { runtime: { type: "langgraph" }, bindings: {} },
      },
      builds: [{ id: "build-1", status: "SUCCEEDED", bundleDigest: "sha256:bundle" }],
    }));
  }
  if (path === "/api/v1/catalog/resources?limit=200") return new Response(JSON.stringify({ items: [] }));
  if (path === "/api/v1/deployments") return new Response(JSON.stringify({ items: deploymentItems }));
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { AgentDetailPage } from "./AgentDetailPage";

describe("AgentDetailPage cloud deployment", () => {
  it("opens the unified deployment flow for the exact successful Build", async () => {
    deploymentItems = [];
    window.location.hash = "#/agents/demo-agent";
    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "部署到云端" }));

    expect(window.location.hash).toBe("#/deployments/new?buildId=build-1&agentId=demo-agent");
    expect(apiFetch).not.toHaveBeenCalledWith(
      "/api/v1/builds/build-1/deployments",
      expect.anything(),
    );
    expect(screen.queryByText(/preproduction|不上传代码包/)).not.toBeInTheDocument();
  });

  it("shows a ready cloud receipt instead of offering a duplicate deployment", async () => {
    deploymentItems = [{
      id: "dep-1", buildId: "build-1", agentId: "ar-cloud-1", status: "READY",
    }];

    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    expect(await screen.findByText("云端实例运行中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看云端部署" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看云端部署" }));
    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-1"));
  });
});
