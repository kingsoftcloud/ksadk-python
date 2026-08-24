import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
let buildStatus = "SUCCEEDED";
let runtimeType = "langgraph";
let artifactType = "Code";

apiFetch.mockImplementation(async (path: string) => {
  if (path === "/api/v1/agents/demo-agent") {
    return new Response(JSON.stringify({
      draft: {
        metadata: {
          id: "demo-agent",
          revision: 3,
          labels: { "agentkit.ksyun.com/artifact-type": artifactType },
        },
        spec: { runtime: { type: runtimeType } },
      },
      builds: [{
        id: "build-3",
        status: buildStatus,
        bundleDigest: "sha256:bundle-3",
        resolvedDigest: "sha256:resolved-3",
        runtimeName: "LangGraph",
        runtimeVersion: "1",
      }],
    }));
  }
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { BuildsPage } from "./BuildsPage";

describe("BuildsPage", () => {
  beforeEach(() => {
    buildStatus = "SUCCEEDED";
    runtimeType = "langgraph";
    artifactType = "Code";
    window.location.hash = "#/builds";
  });

  it("shows immutable Bundle facts rather than an inferred cloud status", async () => {
    const onSelectAgent = vi.fn();
    render(
      <BuildsPage
        currentAgentId="demo-agent"
        agents={[{ metadata: { id: "demo-agent", name: "Demo Agent", revision: 3 } }]}
        onSelectAgent={onSelectAgent}
        onCreate={vi.fn()}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Code Bundle" })).toBeInTheDocument();
    expect(screen.getAllByText("sha256:bundle-3")).toHaveLength(2);
    expect(screen.getByText("构建完成，下一步可部署到云端")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "部署到云端" }));

    expect(onSelectAgent).toHaveBeenCalledWith("demo-agent");
    expect(window.location.hash).toBe("#/deployments/new?buildId=build-3&agentId=demo-agent");
  });

  it("does not offer cloud deployment before a successful Build exists", async () => {
    buildStatus = "FAILED";
    render(
      <BuildsPage
        currentAgentId="demo-agent"
        agents={[{ metadata: { id: "demo-agent", name: "Demo Agent", revision: 3 } }]}
        onSelectAgent={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    expect(await screen.findByText("等待构建完成")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "部署到云端" })).not.toBeInTheDocument();
  });

  it("distinguishes a Codex ManagedRuntime declaration from a Code Bundle", async () => {
    runtimeType = "codex";
    artifactType = "ManagedRuntime";

    render(
      <BuildsPage
        currentAgentId="demo-agent"
        agents={[{ metadata: { id: "demo-agent", name: "Demo Agent", revision: 3 } }]}
        onSelectAgent={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    expect(await screen.findByRole("heading", { name: "ManagedRuntime 声明" })).toBeInTheDocument();
    expect(screen.getByText(/托管运行时制品/)).toBeInTheDocument();
    expect(screen.queryByText(/不上传代码包/)).not.toBeInTheDocument();
  });
});
