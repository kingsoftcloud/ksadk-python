import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));

apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
  if (path === "/api/v1/deployments") {
    return new Response(JSON.stringify({ items: [{
      id: "dep-instance-1",
      buildId: "build-current",
      bundleDigest: "sha256:bundle-current",
      versionId: "cloud-agent-1",
      status: "READY",
      target: { region: "cn-beijing-6", environment: "preproduction" },
      agentId: "ar-cloud-ui",
      instanceId: "instance-1",
    }] }));
  }
  if (path === "/api/v1/deployments/dep-instance-1") {
    return new Response(JSON.stringify({
      id: "dep-instance-1", buildId: "build-current", bundleDigest: "sha256:bundle-current",
      versionId: "cloud-agent-1", status: "DEPLOYING",
      target: { region: "cn-beijing-6", environment: "preproduction" }, instanceId: "instance-1",
    }));
  }
  if (path === "/api/v1/builds/build-current") return new Response(JSON.stringify({ agentId: "demo-agent" }));
  if (path === "/api/v1/agents/demo-agent") return new Response(JSON.stringify({
    builds: [
      { id: "build-current", status: "SUCCEEDED" },
      { id: "build-previous", status: "SUCCEEDED" },
    ],
  }));
  if (path === "/api/v1/deployments/dep-instance-1:rollback") {
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ targetBuildId: "build-previous" });
    return new Response(JSON.stringify({ id: "operation-rollback" }));
  }
  if (path === "/api/v1/deployments/dep-instance-1:dashboard") {
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify({ accessUrl: "https://dashboard.example.test/private-link" }));
  }
  if (path === "/api/v1/operations/operation-rollback") {
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "dep-instance-2" }));
  }
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { DeploymentsPage } from "./DeploymentsPage";

describe("DeploymentsPage", () => {
  it("refreshes a Server-projected status and rolls back to an explicit historical Build", async () => {
    render(<DeploymentsPage onCreate={vi.fn()} />);

    expect(await screen.findByText("instance-1")).toBeInTheDocument();
    expect(screen.queryByText("preproduction")).not.toBeInTheDocument();
    expect(screen.queryByText("cn-beijing-6")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新部署状态" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/deployments/dep-instance-1"));
    expect(await screen.findByText("部署中")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "选择回滚 Build" }));
    expect(await screen.findByLabelText("选择回滚目标 Build")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("选择回滚目标 Build"), { target: { value: "build-previous" } });
    fireEvent.click(screen.getByRole("button", { name: "提交回滚" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/deployments/dep-instance-1:rollback",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("opens only the receipt-bound Agent Hosted UI link", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    render(<DeploymentsPage onCreate={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "打开该 Agent 的云端 UI" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/deployments/dep-instance-1:dashboard",
      { method: "POST" },
    ));
    expect(open).toHaveBeenCalledWith(
      "https://dashboard.example.test/private-link",
      "_blank",
      "noopener,noreferrer",
    );
    vi.unstubAllGlobals();
  });
});
