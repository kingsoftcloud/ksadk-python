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
    expect(screen.getByText("添加附件")).toBeInTheDocument();
    expect(screen.getByText("本轮最多 4 个")).toBeInTheDocument();
    expect(screen.getByLabelText("选择本轮附件")).toHaveAttribute("tabindex", "-1");
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

  it("hides optional controls that the active conversation surface does not declare", () => {
    render(
      <ChatComposer
        input="/"
        placeholder="输入消息"
        disabled={false}
        active
        attachments={[]}
        mode="default"
        approvalMode="risk"
        models={[{ id: "qwen3.7-flash", label: "qwen3.7-flash", reasoningEfforts: ["high"] }]}
        model="qwen3.7-flash"
        reasoningEffort=""
        canSend
        allowAttachments={false}
        allowPlan={false}
        allowGoal={false}
        allowApproval={false}
        allowModelSelection={false}
        allowReasoning={false}
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

    expect(screen.queryByRole("button", { name: "添加附件或运行控制" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批准模式/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /模型|推理强度/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("斜杠命令")).not.toBeInTheDocument();
  });

  it("can expose reasoning without exposing model selection", async () => {
    const user = userEvent.setup();
    render(
      <ChatComposer
        input=""
        placeholder="输入消息"
        disabled={false}
        active
        attachments={[]}
        mode="default"
        approvalMode="risk"
        models={[{ id: "reasoning-model", label: "Reasoning Model", reasoningEfforts: ["low", "high"] }]}
        model="reasoning-model"
        reasoningEffort=""
        canSend={false}
        allowModelSelection={false}
        allowReasoning
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

    await user.click(screen.getByRole("button", { name: "推理强度 自动" }));
    expect(screen.queryByRole("menuitem", { name: /模型/ })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /推理强度.*自动/ })).toBeInTheDocument();
  });
});
