import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));

apiFetch.mockImplementation(async (path: string) => {
  if (path === "/api/v1/agents/demo-agent") {
    return new Response(JSON.stringify({
      draft: { metadata: { id: "demo-agent", revision: 3 }, spec: { runtime: { type: "langgraph" } } },
      builds: [{
        id: "build-3",
        status: "SUCCEEDED",
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
  it("shows immutable Bundle facts rather than an inferred cloud status", async () => {
    render(
      <BuildsPage
        currentAgentId="demo-agent"
        agents={[{ metadata: { id: "demo-agent", name: "Demo Agent", revision: 3 } }]}
        onSelectAgent={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    expect(await screen.findByRole("heading", { name: "不可变 Bundle" })).toBeInTheDocument();
    expect(screen.getAllByText("sha256:bundle-3")).toHaveLength(2);
    expect(screen.getByText("尚未部署")).toBeInTheDocument();
  });
});
