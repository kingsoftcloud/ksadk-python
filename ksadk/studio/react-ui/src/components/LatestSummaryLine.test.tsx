import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LatestSummaryLine } from "./LatestSummaryLine";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
const preview = () => screen.getByTestId("thinking-line-scroll").textContent;

it("keeps the next line and newest queued line, refreshing one key in place", () => {
  vi.useFakeTimers();
  const { rerender, unmount } = render(<LatestSummaryLine text="first" streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond"} streaming />);
  expect(preview()).toBe("second");
  rerender(<LatestSummaryLine text={"first\nsecond\nthird"} streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird\nfourth"} streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird\nfourth\nfifth"} streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird\nfourth\nfifth grows"} streaming />);
  act(() => vi.advanceTimersByTime(800));
  expect(preview()).toBe("third");
  act(() => vi.advanceTimersByTime(800));
  expect(preview()).toBe("fifth grows");
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it("skips stale queued lines when the animation timer resumes late", () => {
  vi.useFakeTimers(); vi.setSystemTime(0);
  const { rerender } = render(<LatestSummaryLine text="first" streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond"} streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird"} streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird\nfourth"} streaming />);
  // Simulate a suspended tab: the first animation callback runs well beyond
  // the complete transition + hold interval, not a sequence of timely ticks.
  vi.setSystemTime(2000);
  act(() => vi.advanceTimersByTime(300));
  expect(preview()).toBe("fourth");
});

it("shows the newest line immediately for reduced motion and at completion", () => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const { rerender } = render(<LatestSummaryLine text="first" streaming />);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird"} streaming />);
  expect(preview()).toBe("third");
  expect(vi.getTimerCount()).toBe(0);
  rerender(<LatestSummaryLine text={"first\nsecond\nthird\nfinal"} streaming={false} />);
  expect(preview()).toBe("final");
});
