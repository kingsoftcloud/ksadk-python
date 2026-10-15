import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RunSteeringControl } from "./RunSteeringControl";

describe("active Run steering", () => {
  it("submits once to the captured Run and clears only after acceptance", async () => {
    let release!: () => void;
    const submit = vi.fn(() => new Promise<void>(resolve => { release = resolve; }));
    const accepted = vi.fn();
    render(<RunSteeringControl runId="run-1" submit={submit} onAccepted={accepted} />);
    fireEvent.change(screen.getByLabelText("调整当前任务"), { target: { value: "检查失败的测试" } });
    fireEvent.click(screen.getByRole("button", { name: "发送纠偏" }));
    expect(submit).toHaveBeenCalledExactlyOnceWith("run-1", "检查失败的测试");
    expect(screen.getByLabelText("调整当前任务")).toHaveValue("检查失败的测试");
    await act(async () => release());
    expect(accepted).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("调整当前任务")).toHaveValue("");
    expect(screen.getByRole("status")).toHaveTextContent("已接收");
  });

  it("keeps a rejected correction for explicit retry instead of starting another Run", async () => {
    const submit = vi.fn().mockRejectedValue(new Error("本轮已结束"));
    const accepted = vi.fn();
    render(<RunSteeringControl runId="run-1" submit={submit} onAccepted={accepted} />);
    fireEvent.change(screen.getByLabelText("调整当前任务"), { target: { value: "保留现有文件" } });
    fireEvent.keyDown(screen.getByLabelText("调整当前任务"), { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("本轮已结束"));
    expect(screen.getByLabelText("调整当前任务")).toHaveValue("保留现有文件");
    expect(accepted).not.toHaveBeenCalled();
    expect(submit).toHaveBeenCalledOnce();
  });

  it("does not project a late receipt into another Run", async () => {
    let release!: () => void;
    const submit = vi.fn(() => new Promise<void>(resolve => { release = resolve; }));
    const accepted = vi.fn();
    const { rerender } = render(<RunSteeringControl runId="old" submit={submit} onAccepted={accepted} />);
    fireEvent.change(screen.getByLabelText("调整当前任务"), { target: { value: "旧任务纠偏" } });
    fireEvent.click(screen.getByRole("button", { name: "发送纠偏" }));
    rerender(<RunSteeringControl runId="new" submit={submit} onAccepted={accepted} />);
    await act(async () => release());
    expect(accepted).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByLabelText("调整当前任务")).toHaveValue("");
  });
});
