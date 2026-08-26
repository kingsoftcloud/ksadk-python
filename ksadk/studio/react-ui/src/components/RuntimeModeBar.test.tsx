import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { RuntimeModeBar } from "./RuntimeModeBar";

describe("RuntimeModeBar", () => {
  test("shows a running goal with its start time and controls", async () => {
    const pause = vi.fn();
    const stop = vi.fn();
    const startedAt = "2026-08-11T00:20:00+08:00";
    const expectedStart = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(startedAt));
    render(
      <RuntimeModeBar
        mode="goal"
        status="running"
        objective="完成 Studio 交互重构"
        startedAt={startedAt}
        now={new Date("2026-08-11T00:22:03+08:00").getTime()}
        onPause={pause}
        onStop={stop}
      />,
    );

    expect(screen.getByTestId("runtime-mode-bar")).toHaveTextContent("目标执行中");
    expect(screen.getByTestId("runtime-mode-bar")).toHaveTextContent("完成 Studio 交互重构");
    expect(screen.getByTestId("runtime-mode-bar")).toHaveTextContent(`${expectedStart} 启动`);
    expect(screen.getByTestId("runtime-mode-bar")).toHaveTextContent("2分 3秒");
    await userEvent.click(screen.getByRole("button", { name: "暂停目标" }));
    await userEvent.click(screen.getByRole("button", { name: "结束目标" }));
    expect(pause).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(1);
  });

  test("offers resume for a paused plan", async () => {
    const resume = vi.fn();
    render(
      <RuntimeModeBar
        mode="plan"
        status="paused"
        objective="先分析再给方案"
        startedAt="2026-08-11T00:20:00+08:00"
        elapsedMs={9_000}
        onResume={resume}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByTestId("runtime-mode-bar")).toHaveTextContent("计划已暂停");
    expect(screen.getByTestId("runtime-mode-bar")).toHaveTextContent("9秒");
    await userEvent.click(screen.getByRole("button", { name: "继续计划" }));
    expect(resume).toHaveBeenCalledTimes(1);
  });
});
