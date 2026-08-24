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
    expect(screen.getByText("添加图片或文本")).toBeInTheDocument();
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
    expect(screen.getByText("添加图片或文本")).toBeInTheDocument();

    rerender(<ComposerActionMenu {...props} active={false} />);
    expect(screen.queryByText("添加图片或文本")).not.toBeInTheDocument();
  });
});
