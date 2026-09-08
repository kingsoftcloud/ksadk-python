import { useEffect, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Command } from "cmdk";
import { ListTodo, Paperclip, Plus, Target, Undo2 } from "lucide-react";
import {
  COMPOSER_ATTACHMENT_ACCEPT,
  visibleComposerCommands,
  type ComposerCommand,
} from "../composerActions";

interface ComposerActionMenuProps {
  disabled: boolean;
  onTogglePlan: () => void;
  onStartGoal: () => void;
  onFiles: (files: File[]) => void;
  active?: boolean;
  allowAttachments?: boolean;
  allowPlan?: boolean;
  allowGoal?: boolean;
  attachmentAccept?: string;
  attachmentLimit?: number;
}

function CommandIcon({ id }: { id: ComposerCommand["id"] }) {
  if (id === "goal") return <Target size={16} />;
  if (id === "default") return <Undo2 size={16} />;
  return <ListTodo size={16} />;
}

export function ComposerActionMenu({
  disabled,
  onTogglePlan,
  onStartGoal,
  onFiles,
  active = true,
  allowAttachments = true,
  allowPlan = true,
  allowGoal = true,
  attachmentAccept = COMPOSER_ATTACHMENT_ACCEPT,
  attachmentLimit = 4,
}: ComposerActionMenuProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!active) setOpen(false);
  }, [active]);
  if (!allowAttachments && !allowPlan && !allowGoal) return null;
  return (
    <>
      {allowAttachments && (
        <input
          ref={fileInputRef}
          className="composer-file-input"
          type="file"
          tabIndex={-1}
          aria-label="选择本轮附件"
          multiple
          accept={attachmentAccept || undefined}
          onChange={event => {
            const files = [...(event.target.files || [])];
            event.target.value = "";
            if (files.length) onFiles(files);
          }}
        />
      )}
      <DropdownMenu.Root open={open} onOpenChange={setOpen}>
        <DropdownMenu.Trigger asChild>
          <button
            className="chat-plus-trigger"
            type="button"
            disabled={disabled}
            aria-label="添加附件或运行控制"
            title="添加附件或运行控制"
          >
            <Plus size={17} />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content className="composer-action-menu" side="top" align="start" sideOffset={10} collisionPadding={12}>
            {allowAttachments && (
              <>
                <DropdownMenu.Label className="composer-action-heading">添加到本轮</DropdownMenu.Label>
                <DropdownMenu.Item className="composer-action-item" onSelect={() => fileInputRef.current?.click()}>
                  <Paperclip size={16} />
                  <span><strong>添加附件</strong><small>本轮最多 {attachmentLimit} 个</small></span>
                </DropdownMenu.Item>
              </>
            )}
            {allowAttachments && (allowPlan || allowGoal) && (
              <DropdownMenu.Separator className="composer-action-separator" />
            )}
            {(allowPlan || allowGoal) && (
              <DropdownMenu.Label className="composer-action-heading">运行方式</DropdownMenu.Label>
            )}
            {allowPlan && (
              <DropdownMenu.Item className="composer-action-item" onSelect={onTogglePlan}>
                <ListTodo size={16} />
                <span><strong>计划模式</strong><small>下一轮使用 Codex Plan</small></span>
              </DropdownMenu.Item>
            )}
            {allowGoal && (
              <DropdownMenu.Item className="composer-action-item" onSelect={onStartGoal}>
                <Target size={16} />
                <span><strong>设定长期目标</strong><small>朝可验证的停止条件持续推进</small></span>
              </DropdownMenu.Item>
            )}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </>
  );
}

export function ComposerCommandMenu({
  input,
  activeIndex,
  onSelect,
  allowPlan = true,
  allowGoal = true,
}: {
  input: string;
  activeIndex: number;
  onSelect: (id: ComposerCommand["id"]) => void;
  allowPlan?: boolean;
  allowGoal?: boolean;
}) {
  const commands = visibleComposerCommands(input).filter(command => (
    (command.id !== "plan" || allowPlan)
    && (command.id !== "goal" || allowGoal)
    && (command.id !== "default" || allowPlan)
  ));
  if (!commands.length) return null;
  return (
    <Command className="composer-command-menu" shouldFilter={false} aria-label="斜杠命令">
      <Command.List>
        {commands.map((command, index) => (
          <Command.Item
            key={command.id}
            value={command.id}
            data-active={index === activeIndex ? "true" : "false"}
            onSelect={() => onSelect(command.id)}
          >
            <CommandIcon id={command.id} />
            <span><strong>{command.label}</strong><small>{command.description}</small></span>
            <kbd>{command.slash}</kbd>
          </Command.Item>
        ))}
      </Command.List>
    </Command>
  );
}
