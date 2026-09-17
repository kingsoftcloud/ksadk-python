import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentConversationComposer } from "@kingsoftcloud/ksadk-web/chat/composer";
import { ChatAgentSelector } from "./ChatAgentSelector";

describe("shared composer host controls", () => {
  it("lets the Agent picker handle Enter without submitting the message draft", async () => {
    const user = userEvent.setup();
    const send = vi.fn(async () => {});
    const changeAgent = vi.fn();
    render(<AgentConversationComposer composerMaxHeight={176} isMobile={false}
      submitDraft={send} stopGeneration={() => {}}
      headerSlot={<ChatAgentSelector value="local-a" onValueChange={changeAgent} options={[
        { value: "local-a", label: "工作助手", group: "本地" },
        { value: "cloud-b", label: "代码助手", group: "云端" },
      ]} />} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "保留这段草稿" } });
    await user.click(screen.getByRole("button", { name: "切换对话 Agent" }));
    await user.type(screen.getByRole("combobox", { name: "搜索 Agent" }), "代码");
    await user.keyboard("{Enter}");
    expect(changeAgent).toHaveBeenCalledWith("cloud-b");
    expect(send).not.toHaveBeenCalled();
    expect(input).toHaveValue("保留这段草稿");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(send).toHaveBeenCalledWith("保留这段草稿", [], undefined, undefined, undefined);
  });
});
