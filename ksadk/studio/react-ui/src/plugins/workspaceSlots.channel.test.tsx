import { render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkspaceContributions } from "./workspaceSlots";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("../api", () => ({ apiFetch: fetchMock }));

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

function Host() {
  const pages = useWorkspaceContributions();
  return <nav>{pages.map(page => <span key={page.id}>{page.label}</span>)}</nav>;
}

beforeEach(() => {
  delete window.__STUDIO_DSH__;
  vi.useFakeTimers();
  fetchMock.mockImplementation(async (url: string) => url.includes("workspace-contributions")
    ? json({ items: [{ id: "channels", label: "消息渠道", pluginId: "@kingsoftcloud/dsh-channels-client", order: 45 }] })
    : json({ available: false, enabled: false, health: "disabled" }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.__STUDIO_DSH__;
});

describe("workspace contribution discovery", () => {
  it("discovers the Channel page in the plain Studio shell", async () => {
    render(<Host />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("消息渠道")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/plugins/workspace-contributions");
  });

  it("removes a Channel page after the backend reports it disabled", async () => {
    render(<Host />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("消息渠道")).toBeInTheDocument();
    fetchMock.mockImplementation(async (url: string) => url.includes("workspace-contributions")
      ? json({ items: [] })
      : json({ available: false, enabled: false, health: "disabled" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText("消息渠道")).not.toBeInTheDocument();
  });
});
