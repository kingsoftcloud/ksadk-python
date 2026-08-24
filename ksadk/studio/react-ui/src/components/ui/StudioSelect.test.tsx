import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { StudioSelect } from "./StudioSelect";

describe("StudioSelect", () => {
  it("uses an accessible shared popup and reports the selected value", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(
      <StudioSelect
        ariaLabel="选择运行时"
        value="codex"
        onValueChange={onValueChange}
        options={[
          { value: "codex", label: "Codex" },
          { value: "adk", label: "Google ADK", description: "Python Runtime" },
          { value: "cloud", label: "Cloud", disabled: true },
        ]}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "选择运行时" }));
    await user.click(screen.getByRole("option", { name: /Google ADK/ }));
    expect(onValueChange).toHaveBeenCalledWith("adk");
  });

  it("renders placeholder and disabled state without transparent native menus", () => {
    render(
      <StudioSelect
        ariaLabel="筛选状态"
        value=""
        placeholder="全部状态"
        disabled
        onValueChange={() => undefined}
        options={[{ value: "ready", label: "就绪" }]}
      />,
    );
    const trigger = screen.getByRole("combobox", { name: "筛选状态" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("全部状态");
  });

  it("keeps the complete selected target discoverable when the trigger is compact", () => {
    render(
      <StudioSelect
        className="header-agent-selector"
        ariaLabel="切换会话目标"
        value="cloud:dep-long"
        onValueChange={() => undefined}
        options={[{
          value: "cloud:dep-long",
          label: "云端 · very-long-agent-name-that-cannot-fit-in-the-header",
        }]}
      />,
    );

    expect(screen.getByRole("combobox", { name: "切换会话目标" })).toHaveAttribute(
      "title",
      "云端 · very-long-agent-name-that-cannot-fit-in-the-header",
    );
  });

  it("can open, close, and open the conversation target again", async () => {
    const user = userEvent.setup();
    render(
      <StudioSelect
        className="header-agent-selector"
        ariaLabel="切换会话目标"
        value="local:one"
        onValueChange={() => undefined}
        options={[
          { value: "local:one", label: "本地 · One" },
          { value: "cloud:two", label: "云端 · Two" },
        ]}
      />,
    );

    const trigger = screen.getByRole("combobox", { name: "切换会话目标" });
    await user.click(trigger);
    expect(screen.getByRole("option", { name: "云端 · Two" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("option", { name: "云端 · Two" })).not.toBeInTheDocument();
    await user.click(trigger);
    expect(screen.getByRole("option", { name: "云端 · Two" })).toBeInTheDocument();
  });
});
