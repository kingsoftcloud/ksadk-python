import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ComposerActionMenu, ComposerCommandMenu } from "./ComposerActionMenu";

describe("ComposerActionMenu", () => {
  it("uses the plus button as the shared attachment and runtime-control entry", async () => {
    const user = userEvent.setup();
    const togglePlan = vi.fn();
    const startGoal = vi.fn();
    render(
      <ComposerActionMenu
        disabled={false}
        onTogglePlan={togglePlan}
        onStartGoal={startGoal}
        onFiles={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: "添加附件或运行控制" }));
    expect(screen.getByText("添加附件")).toBeInTheDocument();
    expect(screen.getByText("本轮最多 4 个")).toBeInTheDocument();
    await user.click(screen.getByText("计划模式"));
    expect(togglePlan).toHaveBeenCalledTimes(1);
  });

  it("renders the same commands when slash input opens the command panel", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    render(<ComposerCommandMenu input="/go" activeIndex={0} onSelect={select} />);

    expect(screen.getByText("设定长期目标")).toBeInTheDocument();
    expect(screen.queryByText("计划模式")).not.toBeInTheDocument();
    await user.click(screen.getByText("设定长期目标"));
    expect(select).toHaveBeenCalledWith("goal");
  });

  it("closes its portal when the conversation route becomes inactive", async () => {
    const user = userEvent.setup();
    const props = {
      disabled: false,
      onTogglePlan: vi.fn(),
      onStartGoal: vi.fn(),
      onFiles: vi.fn(),
    };
    const { rerender } = render(<ComposerActionMenu {...props} active />);
    await user.click(screen.getByRole("button", { name: "添加附件或运行控制" }));
    expect(screen.getByText("添加附件")).toBeInTheDocument();

    rerender(<ComposerActionMenu {...props} active={false} />);
    expect(screen.queryByText("添加附件")).not.toBeInTheDocument();
  });

  it("renders only actions permitted by the active conversation surface", async () => {
    const user = userEvent.setup();
    render(
      <ComposerActionMenu
        disabled={false}
        allowAttachments={false}
        allowPlan={false}
        allowGoal
        onTogglePlan={vi.fn()}
        onStartGoal={vi.fn()}
        onFiles={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "添加附件或运行控制" }));
    expect(screen.queryByText("添加附件")).not.toBeInTheDocument();
    expect(screen.queryByText("计划模式")).not.toBeInTheDocument();
    expect(screen.getByText("设定长期目标")).toBeInTheDocument();
  });

  it("removes the plus entry when the surface declares no matching action", () => {
    render(
      <ComposerActionMenu
        disabled={false}
        allowAttachments={false}
        allowPlan={false}
        allowGoal={false}
        onTogglePlan={vi.fn()}
        onStartGoal={vi.fn()}
        onFiles={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "添加附件或运行控制" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("选择本轮附件")).not.toBeInTheDocument();
  });
});
