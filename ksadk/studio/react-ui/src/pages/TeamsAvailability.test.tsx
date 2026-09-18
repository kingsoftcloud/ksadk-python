import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { TeamsAvailability } from "./TeamsAvailability";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
const lifecycle = (health: string, reason?: string) => new Response(JSON.stringify({
  enabled: health === "ready", health, reason, apiVersion: "fixture", authorityRef: "fixture",
}));

beforeEach(() => { vi.useFakeTimers(); fetchMock.mockReset(); });
afterEach(() => vi.useRealTimers());

it("tracks background activation until the team can be opened without enabling again", async () => {
  fetchMock.mockResolvedValueOnce(lifecycle("preparing")).mockResolvedValueOnce(lifecycle("ready"));
  const view = render(<TeamsAvailability />);
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByRole("button", { name: "正在准备团队…" })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(screen.getByRole("button", { name: "打开团队" })).toBeEnabled();
  expect(fetchMock.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("explains an existing workspace owner instead of asking for first-time activation", async () => {
  fetchMock.mockResolvedValueOnce(lifecycle("error", "authority_in_use"));
  render(<TeamsAvailability />);
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByRole("alert")).toHaveTextContent("正在另一个 Studio 中运行");
  expect(screen.getByRole("button", { name: "重试准备团队" })).toBeEnabled();
});
