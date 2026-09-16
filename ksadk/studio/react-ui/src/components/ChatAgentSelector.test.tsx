import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChatAgentSelector, type ChatAgentOption } from "./ChatAgentSelector";

const options: ChatAgentOption[] = [
  { value: "local:writer", label: "写作助手", group: "本地" },
  { value: "cloud:reviewer", label: "Review Agent", group: "云端" },
];

describe("ChatAgentSelector", () => {
  it("groups targets and supports searching and switching with the keyboard", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<ChatAgentSelector value="local:writer" options={options} onValueChange={onValueChange} />);
    await user.click(screen.getByRole("button", { name: "切换对话 Agent" }));
    expect(screen.getByRole("group", { name: "本地" })).toBeVisible();
    expect(screen.getByRole("group", { name: "云端" })).toBeVisible();
    const search = screen.getByRole("combobox", { name: "搜索 Agent" });
    await user.type(search, "review");
    expect(screen.queryByRole("option", { name: /写作助手/ })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Review Agent" })).toBeVisible();
    await user.keyboard("{Enter}");
    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("cloud:reviewer");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("handles no matches and resets the search when reopened without switching", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(<ChatAgentSelector value="local:writer" options={options} onValueChange={onValueChange} />);
    const trigger = screen.getByRole("button", { name: "切换对话 Agent" });
    await user.click(trigger);
    await user.type(screen.getByRole("combobox", { name: "搜索 Agent" }), "unknown-agent");
    expect(screen.getByText("没有找到匹配的 Agent")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    expect(screen.getByRole("combobox", { name: "搜索 Agent" })).toHaveValue("");
    await user.click(within(screen.getByRole("group", { name: "本地" })).getByRole("option"));
    expect(onValueChange).not.toHaveBeenCalled();
  });
});
