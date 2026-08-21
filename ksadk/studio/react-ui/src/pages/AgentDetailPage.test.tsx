import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
let operationPolls = 0;
let deploymentItems: unknown[] = [];

apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
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
  if (path === "/api/v1/system/settings") return new Response(JSON.stringify({ cloudRegion: "cn-beijing-6" }));
  if (path === "/api/v1/builds/build-1/deployments") {
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      target: { region: "cn-beijing-6", environment: "preproduction" },
    });
    return new Response(JSON.stringify({ id: "operation-1", status: "RUNNING" }));
  }
  if (path === "/api/v1/operations/operation-1") {
    operationPolls += 1;
    return new Response(JSON.stringify(
      operationPolls === 1
        ? { status: "RUNNING" }
        : { status: "SUCCEEDED", resourceId: "dep-1" },
    ));
  }
  if (path === "/api/v1/deployments/dep-1") return new Response(JSON.stringify({ instanceId: "instance-1" }));
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { AgentDetailPage } from "./AgentDetailPage";

describe("AgentDetailPage cloud deployment", () => {
  it("submits the latest successful Bundle to the cloud target", async () => {
    operationPolls = 0;
    deploymentItems = [];
    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onOpenDeployments={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "部署到云端" }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/v1/builds/build-1/deployments",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(await screen.findByText("云端处理中：校验 YAML 声明并创建 Agent")).toBeInTheDocument();
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
        onOpenDeployments={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    expect(await screen.findByText("云端实例运行中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看云端部署" })).toBeInTheDocument();
  });
});
