import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { RunElapsed } from "./RunElapsed";
import { LiveThinking } from "./LiveThinking";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => { vi.useRealTimers(); vi.resetAllMocks(); });
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

it("shows the persisted run start before the first model token and survives a page remount", async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-14T07:01:30Z"));
  vi.mocked(apiFetch).mockImplementation(async () => new Response(JSON.stringify({ items: [
    { id: "run-a", status: "RUNNING", startedAt: "2026-10-14T07:00:00Z" },
  ] })));
  const first = render(<RunElapsed runId="run-a" sessionId="ses-a" />); await flush();
  expect(screen.getByTestId("run-elapsed")).toHaveTextContent("工作中 1 分 30 秒");
  first.unmount();
  vi.setSystemTime(new Date("2026-10-14T07:02:00Z"));
  render(<RunElapsed runId="run-a" sessionId="ses-a" />); await flush();
  expect(screen.getByTestId("run-elapsed")).toHaveTextContent("工作中 2 分 0 秒");
});

it("freezes cancelled or completed duration at the recorded boundary", async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-14T09:00:00Z"));
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ items: [
    { id: "run-a", status: "CANCELLED", startedAt: "2026-10-14T07:00:00Z", completedAt: "2026-10-14T07:01:23Z" },
  ] })));
  render(<RunElapsed runId="run-a" sessionId="ses-a" />); await flush();
  expect(screen.getByTestId("run-elapsed")).toHaveTextContent("已取消 1 分 23 秒");
  act(() => vi.advanceTimersByTime(60000));
  expect(screen.getByTestId("run-elapsed")).toHaveTextContent("已取消 1 分 23 秒");
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it("does not apply a late response or the previous timer to another run", async () => {
  let resolve!: (response: Response) => void;
  vi.mocked(apiFetch).mockReturnValueOnce(new Promise(done => { resolve = done; }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ id: "run-b", status: "FAILED" }] })));
  const view = render(<RunElapsed runId="run-a" sessionId="ses-a" />);
  view.rerender(<RunElapsed runId="run-b" sessionId="ses-b" />); await flush();
  resolve(new Response(JSON.stringify({ items: [{ id: "run-a", status: "RUNNING", startedAt: "2026-10-14T07:00:00Z" }] })));
  await flush();
  expect(screen.getByTestId("run-elapsed")).toHaveAttribute("data-run-id", "run-b");
  expect(screen.getByTestId("run-elapsed")).toHaveTextContent(/^执行失败$/);
});

it("distinguishes a stopped task from the durations of later failed questions", async () => {
  vi.mocked(apiFetch).mockImplementation(async () => new Response(JSON.stringify({ items: [
    { id: "run-game", status: "INTERRUPTED", durationMs: 1961000 },
    { id: "run-question", status: "FAILED", durationMs: 500 },
  ] })));
  render(<><RunElapsed runId="run-game" sessionId="ses-a" /><RunElapsed runId="run-question" sessionId="ses-a" /></>);
  await flush();
  expect(screen.getAllByTestId("run-elapsed").map(node => node.textContent)).toEqual([
    "已停止 32 分 41 秒", "执行失败 0 秒",
  ]);
});

it("omits invented zero-second timing when loading historical thinking", () => {
  render(<LiveThinking text="completed reasoning" streaming={false} />);
  expect(screen.getByText("思考")).toBeVisible();
  expect(screen.queryByText(/0 秒|持续了/)).toBeNull();
});

it("keeps a subsecond observed segment truthful and freezes it after completion", () => {
  vi.useFakeTimers(); vi.setSystemTime(1000);
  const view = render(<LiveThinking text="reasoning" streaming />);
  vi.setSystemTime(1250); view.rerender(<LiveThinking text="reasoning" streaming={false} />);
  expect(screen.getByText(/少于 1 秒/)).toBeVisible();
  act(() => vi.advanceTimersByTime(20000));
  expect(screen.getByText(/少于 1 秒/)).toBeVisible();
});
