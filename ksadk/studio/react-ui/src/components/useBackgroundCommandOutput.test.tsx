import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { useBackgroundCommandOutput } from "./useBackgroundCommandOutput";
import { ToolExecutionCard } from "./ToolExecutionCard";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => { vi.useRealTimers(); vi.resetAllMocks(); });
const task = "a".repeat(64);
const observation = (status: string, taskId = task) => new Response(JSON.stringify({ taskId,
  display: { version: "tool-display/v1", category: "terminal", background_task_id: taskId,
    background_status: status, output: `output-${status}`, exit_code: status === "completed" ? 0 : null },
}));

describe("owned background process output", () => {
  it("updates a historical launcher after completion without a new model turn", async () => {
    vi.mocked(apiFetch).mockResolvedValue(observation("completed"));
    render(<ToolExecutionCard runId="run-original" tool={{ id: "tool", callId: "call", name: "run_command", status: "completed",
      display: { version: "tool-display/v1", category: "terminal", summary: "运行命令：original-command",
        background_task_id: task, background_status: "running" } }} />);
    expect(await screen.findByText("后台已完成")).toBeVisible();
    expect(screen.getByText(/original-command/)).toBeVisible();
    expect(screen.queryByText("后台运行")).toBeNull();
    expect(apiFetch).toHaveBeenCalledWith(`/api/v1/runs/run-original/background-commands/${task}/output`,
      expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });
  it("retries the same handle after a transient query failure and stops only on an observed terminal state", async () => {
    vi.useFakeTimers();
    vi.mocked(apiFetch).mockResolvedValueOnce(observation("running"))
      .mockRejectedValueOnce(new Error("connection lost"))
      .mockResolvedValueOnce(observation("cancelled"));
    const { result, unmount } = renderHook(() => useBackgroundCommandOutput("run", task));
    await act(async () => {});
    expect(result.current.display?.background_status).toBe("running");
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.queryFailed).toBe(true);
    expect(result.current.display?.background_status).toBe("running");
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.display?.background_status).toBe("cancelled");
    expect(result.current.queryFailed).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(apiFetch).toHaveBeenCalledTimes(3);
    unmount();
    expect(vi.mocked(apiFetch).mock.calls[0][1]?.signal?.aborted).toBe(true);
  });
  it("does not apply an old owner response after changing runs", async () => {
    let finishOld!: (response: Response) => void;
    vi.mocked(apiFetch).mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
      .mockResolvedValueOnce(observation("completed"));
    const { result, rerender } = renderHook(({ run }) => useBackgroundCommandOutput(run, task), { initialProps: { run: "old" } });
    rerender({ run: "new" });
    await act(async () => {});
    expect(result.current.display?.background_status).toBe("completed");
    await act(async () => { finishOld(observation("failed")); });
    expect(result.current.display?.background_status).toBe("completed");
  });
  it("rejects an observation for a different task instead of declaring completion", async () => {
    vi.useFakeTimers();
    vi.mocked(apiFetch).mockResolvedValue(observation("completed", "b".repeat(64)));
    const { result } = renderHook(() => useBackgroundCommandOutput("run", task));
    await act(async () => {});
    expect(result.current.display).toBeUndefined();
    expect(result.current.queryFailed).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });
  it.each([403, 404])("stops polling after deterministic HTTP %s without inventing termination", async status => {
    vi.useFakeTimers();
    vi.mocked(apiFetch).mockResolvedValueOnce(observation("running"))
      .mockResolvedValueOnce(new Response(null, { status }));
    const { result } = renderHook(() => useBackgroundCommandOutput("run", task));
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(31000); });
    expect(result.current.display?.background_status).toBe("running");
    expect(result.current.queryFailed).toBe(true);
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });
  it.each([
    ["cancelled", "已取消", "cancelled"],
    ["unknown", "后台结果未确认", "unknown"],
    ["timed_out", "后台超时", "failed"],
  ] as const)("separates a successful query RPC from process status %s", (status, label, cssStatus) => {
    render(<ToolExecutionCard tool={{ id: "tool", callId: "call", status: "completed",
      display: { version: "tool-display/v1", category: "terminal", summary: "original-command",
        background_status: status } }} />);
    expect(screen.getByText(label).closest("details")).toHaveAttribute("data-status", cssStatus);
  });
  it("does not treat a cancellation request as a terminal process result", () => {
    render(<ToolExecutionCard tool={{ id: "tool", callId: "call", status: "completed",
      display: { version: "tool-display/v1", category: "terminal", summary: "original-command",
        background_status: "running", background_cancel_requested: true } }} />);
    expect(screen.getByText("正在取消").closest("details")).toHaveAttribute("data-status", "running");
  });
});
