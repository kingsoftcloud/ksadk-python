import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChatComposer } from "./ChatComposer";

function renderComposer(reasoningEfforts: Array<"low" | "medium" | "high"> = []) {
  return render(
    <ChatComposer
      input=""
      placeholder="输入消息"
      disabled={false}
      active
      attachments={[]}
      mode="default"
      approvalMode="risk"
      models={[{ id: "qwen3.7-flash", label: "qwen3.7-flash", reasoningEfforts }]}
      model="qwen3.7-flash"
      reasoningEffort=""
      canSend={false}
      onInputChange={vi.fn()}
      onFiles={vi.fn()}
      onRemoveAttachment={vi.fn()}
      onSetMode={vi.fn()}
      onStartGoal={vi.fn()}
      onApprovalModeChange={vi.fn()}
      onModelChange={vi.fn()}
      onReasoningEffortChange={vi.fn()}
      onCommandSelect={vi.fn()}
      onSend={vi.fn()}
    />,
  );
}

describe("ChatComposer", () => {
  it("keeps Plan, Goal and attachments behind one plus menu without exposing the internal loop", async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(screen.getByRole("button", { name: "添加附件或运行控制" }));
    expect(screen.getByText("添加图片或文本")).toBeInTheDocument();
    expect(screen.queryByText("Agent Loop")).not.toBeInTheDocument();
    expect(screen.getByText("计划模式")).toBeInTheDocument();
    expect(screen.getByText("设定长期目标")).toBeInTheDocument();
  });

  it("renders all three established approval levels in an accessible menu", async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(screen.getByRole("button", { name: "批准模式：帮我批准" }));
    expect(screen.getByRole("menuitemradio", { name: /请求批准/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: /帮我批准/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: /完全访问权限/ })).toBeInTheDocument();
  });

  it("only exposes reasoning effort when the selected model declares the capability", async () => {
    const user = userEvent.setup();
    const { unmount } = renderComposer();
    await user.click(screen.getByRole("button", { name: "模型 qwen3.7-flash" }));
    expect(screen.queryByText("推理强度")).not.toBeInTheDocument();
    unmount();

    renderComposer(["low", "high"]);
    await user.click(screen.getByRole("button", { name: "模型 qwen3.7-flash，推理强度 自动" }));
    expect(screen.getByRole("menuitem", { name: /模型.*qwen3.7-flash/ })).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: /推理强度.*自动/ }));
    expect(await screen.findByRole("menuitemradio", { name: /低/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: /高/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitemradio", { name: /中/ })).not.toBeInTheDocument();
  });
});
