import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
let buildStatus = "SUCCEEDED";

apiFetch.mockImplementation(async (path: string) => {
  if (path === "/api/v1/agents/demo-agent") {
    return new Response(JSON.stringify({
      draft: { metadata: { id: "demo-agent", revision: 3 }, spec: { runtime: { type: "langgraph" } } },
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

    expect(await screen.findByRole("heading", { name: "不可变 Bundle" })).toBeInTheDocument();
    expect(screen.getAllByText("sha256:bundle-3")).toHaveLength(2);
    expect(screen.getByText("构建完成，下一步可部署到云端")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "部署到云端" }));

    expect(onSelectAgent).toHaveBeenCalledWith("demo-agent");
    expect(window.location.hash).toBe("#/agents/demo-agent");
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
});
