import * as Popover from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { Bot, Check, ChevronDown, Search } from "lucide-react";
import { useState } from "react";

export interface ChatAgentOption {
  value: string;
  label: string;
  group: "本地" | "云端";
}

export function ChatAgentSelector({ value, options, onValueChange, placement = "composer" }: {
  value: string;
  options: ChatAgentOption[];
  onValueChange: (value: string) => void;
  placement?: "composer" | "header";
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find(option => option.value === value);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className={`chat-agent-selector${placement === "header" ? " chat-agent-selector--header" : ""}`}
          aria-label="切换对话 Agent"
          title={selected ? `${selected.group} · ${selected.label}` : "选择 Agent"}
        >
          {placement !== "header" && <Bot size={17} aria-hidden="true" />}
          <span>{selected?.label || "选择 Agent"}</span>
          <ChevronDown size={14} aria-hidden="true" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="studio-multi-select-popover chat-agent-selector-popover"
          aria-label="选择对话 Agent"
          side={placement === "header" ? "bottom" : "top"}
          align="start"
          sideOffset={8}
          collisionPadding={12}
        >
          <Command loop label="搜索 Agent">
            <div className="studio-command-search">
              <Search size={14} aria-hidden="true" />
              <Command.Input aria-label="搜索 Agent" placeholder="搜索 Agent…" />
            </div>
            <Command.List className="studio-command-list" label="可用 Agent">
              <Command.Empty>没有找到匹配的 Agent</Command.Empty>
              {(["本地", "云端"] as const).map(group => (
                options.some(option => option.group === group) && (
                  <Command.Group key={group} heading={group}>
                    {options.filter(option => option.group === group).map(option => (
                      <Command.Item
                        key={option.value}
                        value={option.value}
                        keywords={[option.label, option.group]}
                        onSelect={() => {
                          setOpen(false);
                          if (option.value !== value) onValueChange(option.value);
                        }}
                      >
                        <span className="chat-agent-option-name" title={option.label}>{option.label}</span>
                        {option.value === value && <Check size={14} aria-label="当前 Agent" />}
                      </Command.Item>
                    ))}
                  </Command.Group>
                )
              ))}
            </Command.List>
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
