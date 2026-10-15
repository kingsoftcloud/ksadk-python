import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChildRunControls } from "./ChildRunControls";
import { ChildControlError, type ChildInput, type ChildTask } from "../childRunControl";
import type { ControlRun } from "../runControlAdmission";

const run = { agentId: "agent", sessionId: "session", invocationId: "inv", runId: "host-run" };
const child: ChildTask = { child_run_id: "child-1", session_id: "child-session", role: "reviewer",
  phase: "awaiting_interaction", delivery: "foreground", can_promote: true, can_send_input: true, goal_revision: 2 };
function setup() {
  return { childTasks: vi.fn(async (_run: ControlRun, _signal?: AbortSignal) => [child]), promoteChild: vi.fn(async (_run: ControlRun, _child: string, _command: string) => ({
    runId: run.runId, childRunId: child.child_run_id, commandId: "cmd", status: "queued" as const,
  })), sendChildInput: vi.fn(async (_run: ControlRun, _child: string, _input: ChildInput) => ({
    runId: run.runId, childRunId: child.child_run_id, commandId: "cmd", status: "queued" as const,
  })) };
}
const open = async () => {
  fireEvent.click(screen.getByRole("button", { name: "子任务" }));
  await screen.findByLabelText("给reviewer补充说明");
};

describe("Studio child directory controls", () => {
  it("reads only on demand and sends input to the captured original child/version", async () => {
    const api = setup();
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion />);
    expect(api.childTasks).not.toHaveBeenCalled();
    await open();
    expect(screen.getByText("前台 · 等待确认")).toBeVisible();
    fireEvent.change(screen.getByLabelText("给reviewer补充说明"), { target: { value: "核对文件" } });
    fireEvent.click(screen.getByRole("button", { name: "发送补充" }));
    await screen.findByText("补充已排队，等待子任务处理；不会自动批准或恢复暂停。");
    expect(api.sendChildInput).toHaveBeenCalledOnce();
    expect(api.sendChildInput.mock.calls[0]).toEqual([run, "child-1", {
      command_id: expect.stringMatching(/^studio-child:/), text: "核对文件", expected_goal_revision: 2,
    }]);
    expect(screen.getByText("前台 · 等待确认")).toBeVisible();
    expect(api.promoteChild).not.toHaveBeenCalled();
  });
  it("keeps the exact ID/text/revision after an unknown outcome and explicit retry", async () => {
    const api = setup();
    api.sendChildInput.mockRejectedValueOnce(new ChildControlError("投递状态未知", true));
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion />);
    await open();
    const input = screen.getByLabelText("给reviewer补充说明");
    fireEvent.change(input, { target: { value: "原补充" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await screen.findByRole("button", { name: "重试原补充" });
    expect(input).toHaveAttribute("readonly");
    // A new observation cannot rewrite an already submitted payload.
    api.childTasks.mockResolvedValue([{ ...child, goal_revision: 9 }]);
    fireEvent.click(screen.getByRole("button", { name: "刷新子任务" }));
    await waitFor(() => expect(api.childTasks).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "重试原补充" }));
    await screen.findByText("补充已排队，等待子任务处理；不会自动批准或恢复暂停。");
    expect(api.sendChildInput.mock.calls[0]).toEqual(api.sendChildInput.mock.calls[1]);
    expect(input).toHaveValue("");
  });
  it("keeps rejected text for correction without automatic resubmission", async () => {
    const api = setup();
    api.sendChildInput.mockRejectedValueOnce(new ChildControlError("目标已更新"));
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion />);
    await open();
    fireEvent.change(screen.getByLabelText("给reviewer补充说明"), { target: { value: "保留文本" } });
    fireEvent.click(screen.getByRole("button", { name: "发送补充" }));
    await screen.findByText("目标已更新");
    expect(screen.getByLabelText("给reviewer补充说明")).toHaveValue("保留文本");
    expect(api.sendChildInput).toHaveBeenCalledOnce();
  });
  it("submits once while pending and ignores a late receipt after leaving its Run", async () => {
    const api = setup();
    let release!: () => void;
    api.sendChildInput.mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve({ runId: run.runId, childRunId: "child-1", commandId: "cmd", status: "queued" });
    }));
    const { rerender } = render(<ChildRunControls key="old" run={run} api={api} allowInput allowPromotion />);
    await open();
    fireEvent.change(screen.getByLabelText("给reviewer补充说明"), { target: { value: "原运行说明" } });
    fireEvent.click(screen.getByRole("button", { name: "发送补充" }));
    fireEvent.click(screen.getByRole("button", { name: "发送补充" }));
    expect(api.sendChildInput).toHaveBeenCalledOnce();
    rerender(<ChildRunControls key="new" run={{ ...run, runId: "new" }} api={api} allowInput allowPromotion />);
    await act(async () => release());
    expect(screen.queryByText(/补充已排队/)).toBeNull();
    expect(api.childTasks).toHaveBeenCalledOnce();
  });
  it("keeps an uncertain request through collapse and prevents control from stale observations", async () => {
    const api = setup();
    api.sendChildInput.mockRejectedValueOnce(new ChildControlError("状态未知", true));
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion />);
    await open();
    fireEvent.change(screen.getByLabelText("给reviewer补充说明"), { target: { value: "同一说明" } });
    fireEvent.click(screen.getByRole("button", { name: "发送补充" }));
    await screen.findByRole("button", { name: "重试原补充" });
    fireEvent.click(screen.getByRole("button", { name: "子任务" }));
    api.childTasks.mockRejectedValueOnce(new ChildControlError("目录读取失败"));
    fireEvent.click(screen.getByRole("button", { name: "子任务" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "重试原补充" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新子任务" }));
    await screen.findByRole("button", { name: "重试原补充" });
    fireEvent.click(screen.getByRole("button", { name: "重试原补充" }));
    await screen.findByText(/补充已排队/);
    expect(api.sendChildInput.mock.calls[0]).toEqual(api.sendChildInput.mock.calls[1]);
  });
  it("does not turn queue acceptance into a background execution fact", async () => {
    const api = setup();
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion />);
    await open();
    fireEvent.click(screen.getByRole("button", { name: "转到后台" }));
    await screen.findByText("转后台请求已排队，当前状态以运行时返回为准。");
    expect(api.promoteChild).toHaveBeenCalledOnce();
    expect(screen.getByText("前台 · 等待确认")).toBeVisible();
    api.childTasks.mockResolvedValue([{ ...child, delivery: "background", can_promote: false }]);
    fireEvent.click(screen.getByRole("button", { name: "刷新子任务" }));
    await screen.findByText("后台 · 等待确认");
    expect(screen.queryByRole("button", { name: "转到后台" })).toBeNull();
  });
  it("isolates same-role child inputs and hides undeclared controls", async () => {
    const api = setup();
    api.childTasks.mockResolvedValue([child, { ...child, child_run_id: "child-2", can_send_input: false }]);
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion={false} />);
    await open();
    expect(within(screen.getByRole("article", { name: "reviewer · child-2" })).queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "转到后台" })).toBeNull();
  });
  it("detaches a directory read and ignores old Run responses after navigation", async () => {
    const api = setup();
    let release!: (value: ChildTask[]) => void;
    api.childTasks.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    const { rerender } = render(<ChildRunControls key="old" run={run} api={api} allowInput allowPromotion />);
    fireEvent.click(screen.getByRole("button", { name: "子任务" }));
    const signal = api.childTasks.mock.calls[0][1];
    rerender(<ChildRunControls key="new" run={{ ...run, runId: "new" }} api={api} allowInput allowPromotion />);
    expect(signal?.aborted).toBe(true);
    await act(async () => release([child]));
    expect(screen.queryByLabelText("给reviewer补充说明")).toBeNull();
  });
  it("does not poll repeatedly after an owner/authorization rejection", async () => {
    const api = setup();
    api.childTasks.mockRejectedValue(new ChildControlError("运行已结束"));
    render(<ChildRunControls run={run} api={api} allowInput allowPromotion />);
    fireEvent.click(screen.getByRole("button", { name: "子任务" }));
    await screen.findByRole("alert");
    expect(api.childTasks).toHaveBeenCalledOnce();
    expect(api.sendChildInput).not.toHaveBeenCalled();
  });
});

it("cancels only the selected child and retries the original uncertain command without inventing a terminal", async () => {
  const api = { ...setup(), cancelChild: vi.fn()
    .mockRejectedValueOnce(new ChildControlError("取消回执未知", true))
    .mockResolvedValue({ runId: run.runId, childRunId: child.child_run_id, commandId: "cmd", status: "queued" }) };
  api.childTasks.mockResolvedValue([{ ...child, can_cancel: true }, { ...child, child_run_id: "child-2", can_cancel: true }]);
  render(<ChildRunControls run={run} api={api} allowInput={false} allowPromotion={false} allowCancellation />);
  fireEvent.click(screen.getByRole("button", { name: "子任务" }));
  const row = await screen.findByRole("article", { name: "reviewer · child-1" });
  fireEvent.click(within(row).getByRole("button", { name: "取消此子任务" }));
  fireEvent.click(await within(row).findByRole("button", { name: "重试原取消" }));
  await within(row).findByText("取消请求已排队，最终状态以运行时为准。");
  expect(api.cancelChild.mock.calls[0]).toEqual(api.cancelChild.mock.calls[1]);
  expect(api.cancelChild.mock.calls[0]).toEqual([run, "child-1", expect.stringMatching(/^studio-child:/)]);
  expect(within(row).getByText("前台 · 等待确认")).toBeVisible();
  expect(within(row).getByRole("button", { name: "取消此子任务" })).toBeDisabled();
  expect(within(screen.getByRole("article", { name: "reviewer · child-2" })).getByRole("button", { name: "取消此子任务" })).toBeEnabled();
  expect(api.promoteChild).not.toHaveBeenCalled();
  expect(api.sendChildInput).not.toHaveBeenCalled();
});

it("hides child cancellation without both runtime capability and current child permission", async () => {
  const api = { ...setup(), cancelChild: vi.fn() };
  api.childTasks.mockResolvedValue([{ ...child, can_cancel: true }]);
  const { rerender } = render(<ChildRunControls run={run} api={api} allowInput={false} allowPromotion={false} />);
  fireEvent.click(screen.getByRole("button", { name: "子任务" }));
  await screen.findByRole("article");
  expect(screen.queryByRole("button", { name: "取消此子任务" })).toBeNull();
  api.childTasks.mockResolvedValue([{ ...child, can_cancel: false }]);
  fireEvent.click(screen.getByRole("button", { name: "刷新子任务" }));
  await waitFor(() => expect(api.childTasks).toHaveBeenCalledTimes(2));
  rerender(<ChildRunControls run={run} api={api} allowInput={false} allowPromotion={false} allowCancellation />);
  expect(screen.queryByRole("button", { name: "取消此子任务" })).toBeNull();
});
