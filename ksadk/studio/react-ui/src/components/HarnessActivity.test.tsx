import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ActivityRow, HarnessActivity, type Activity } from "./HarnessActivity";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());
const row: Activity = { id: "child-1", label: "调研 Codex", status: "completed", kind: "subagent", details: [{ id: "overview", text: "已搜索 3 次资料，查看 2 个网页" }], children: [
  { id: "search", kind: "step", label: "搜索资料", status: "completed", details: [{ id: "query", text: "搜索资料：Codex 长任务" }] },
] };
describe("expandable human-friendly activity", () => {
  it("expands each child and its steps independently", async () => {
    const user = userEvent.setup();
    render(<><ActivityRow activity={row} /><ActivityRow activity={{ ...row, id: "child-2", label: "调研 ADK", status: "running" }} /></>);
    expect(screen.getAllByText("已搜索 3 次资料，查看 2 个网页")[0]).not.toBeVisible();
    await user.click(screen.getByText("调研 Codex"));
    expect(screen.getAllByText("已搜索 3 次资料，查看 2 个网页")[0]).toBeVisible();
    expect(screen.getAllByText("已搜索 3 次资料，查看 2 个网页")[1]).not.toBeVisible();
    await user.click(screen.getAllByText("搜索资料")[0]);
    expect(screen.getAllByText("搜索资料：Codex 长任务")[0]).toBeVisible();
  });
  it("keeps opened rows when status updates and does not invent missing details", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ActivityRow activity={{ ...row, status: "running", details: [], children: [] }} />);
    await user.click(screen.getByText("调研 Codex"));
    rerender(<ActivityRow activity={{ ...row, details: [], children: [] }} />);
    expect(screen.getByText("这段记录没有保存更细的执行步骤。")).toBeVisible();
    expect(screen.getByText("已完成")).toBeVisible();
  });
  it("loads curated run evidence, rejects unsafe action links", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ runId: "run-1", status: "completed", activities: [row] })));
    render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    expect(await screen.findByText("调研 Codex")).toBeVisible();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/runs/run-1/activities", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    const { container } = render(<ActivityRow activity={{ ...row, children: [], details: [{ id: "bad", text: "查看资料", href: "javascript:alert(1)" }] }} />);
    expect(container.querySelector("a")).toBeNull();
  });
  it("does not retain another run's evidence when changing conversations", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({ runId: "run-1", status: "completed", activities: [row] }))).mockResolvedValueOnce(new Response("", { status: 404 }));
    const { rerender } = render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    expect(await screen.findByText("调研 Codex")).toBeVisible();
    rerender(<HarnessActivity runId="run-2" streaming={false} fallback={[{ ...row, id: "fallback", label: "分析任务", details: [], children: [] }]} />);
    await waitFor(() => expect(screen.queryByText("调研 Codex")).not.toBeInTheDocument());
    expect(await screen.findByText(/详细步骤暂时无法加载/)).toBeVisible();
  });
});
