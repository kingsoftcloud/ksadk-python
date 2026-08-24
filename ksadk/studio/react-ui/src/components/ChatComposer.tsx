import { type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  BrainCircuit,
  Check,
  ChevronDown,
  ChevronRight,
  FileText,
  Hand,
  ListTodo,
  Send,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  APPROVAL_MODES,
  approvalModeOption,
  normalizeApprovalMode,
  type ApprovalMode,
} from "../approvalModes";
import {
  formatAttachmentSize,
  visibleComposerCommands,
  type CollaborationMode,
  type ComposerCommand,
} from "../composerActions";
import { ComposerActionMenu, ComposerCommandMenu } from "./ComposerActionMenu";

export type ReasoningEffort = "" | "low" | "medium" | "high";

export interface ComposerModelOption {
  id: string;
  label: string;
  reasoningEfforts?: ReasoningEffort[];
}

export interface ComposerAttachmentView {
  id: string;
  name: string;
  kind: "image" | "text" | "file";
  size: number;
  previewUrl?: string;
}

const REASONING_OPTIONS: ReadonlyArray<{
  value: ReasoningEffort;
  label: string;
  description: string;
}> = [
  { value: "", label: "自动", description: "使用模型或 Agent 的默认推理强度" },
  { value: "low", label: "低", description: "优先缩短响应时间" },
  { value: "medium", label: "中", description: "平衡速度与推理深度" },
  { value: "high", label: "高", description: "优先更充分的推理" },
];

function ApprovalModeMenu({
  value,
  onChange,
  active,
}: {
  value: ApprovalMode;
  onChange: (value: ApprovalMode) => void;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!active) setOpen(false);
  }, [active]);
  const selected = approvalModeOption(value);
  const icon = value === "ask"
    ? <Hand size={15} />
    : value === "full"
      ? <ShieldAlert size={15} />
      : <ShieldCheck size={15} />;
  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <button
          className={`chat-approval-trigger ${value}`}
          type="button"
          aria-label={`批准模式：${selected.label}`}
          title={`${selected.label}；下一轮生效`}
        >
          {icon}
          <span>{selected.compactLabel}</span>
          <ChevronDown size={13} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="chat-approval-menu"
          side="top"
          sideOffset={9}
          align="start"
          collisionPadding={12}
        >
          <div className="chat-approval-menu-heading">
            <strong>如何批准 Agent 操作？</strong>
            <span>下一轮生效</span>
          </div>
          <DropdownMenu.RadioGroup value={value} onValueChange={next => onChange(normalizeApprovalMode(next))}>
            {APPROVAL_MODES.map(option => (
              <DropdownMenu.RadioItem
                key={option.value}
                value={option.value}
                className={`chat-approval-option ${option.value}`}
              >
                <span className="chat-approval-option-icon">
                  {option.value === "ask" ? <Hand size={17} />
                    : option.value === "full" ? <ShieldAlert size={17} />
                      : <ShieldCheck size={17} />}
                </span>
                <span className="chat-approval-option-copy">
                  <strong>{option.label}</strong>
                  <small>{option.description}</small>
                </span>
                <DropdownMenu.ItemIndicator className="chat-approval-indicator">
                  <Check size={16} />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function ModelReasoningMenu({
  models,
  model,
  reasoningEffort,
  disabled,
  active,
  onModelChange,
  onReasoningEffortChange,
  onConfigure,
}: {
  models: ComposerModelOption[];
  model: string;
  reasoningEffort: ReasoningEffort;
  disabled: boolean;
  active: boolean;
  onModelChange: (value: string) => void;
  onReasoningEffortChange: (value: ReasoningEffort) => void;
  onConfigure?: () => void;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!active) setOpen(false);
  }, [active]);
  const selectedModel = models.find(item => item.id === model);
  const modelLabel = selectedModel?.label || model || "未绑定模型";
  const supportedReasoningEfforts = selectedModel?.reasoningEfforts || [];
  const reasoningSupported = supportedReasoningEfforts.length > 0;
  const effortLabel = REASONING_OPTIONS.find(item => item.value === reasoningEffort)?.label || "自动";
  if (models.length === 0) {
    return (
      <button
        className="chat-model-trigger missing"
        type="button"
        aria-label="当前 Agent 未绑定模型，前往配置"
        title="当前 Agent 未绑定模型"
        onClick={onConfigure}
      >
        <BrainCircuit size={14} />
        <span>未绑定模型</span>
      </button>
    );
  }
  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <button
          className="chat-model-trigger chat-model-summary-trigger"
          type="button"
          disabled={disabled}
          aria-label={reasoningSupported ? `模型 ${modelLabel}，推理强度 ${effortLabel}` : `模型 ${modelLabel}`}
          title="选择模型与推理强度；下一轮生效"
        >
          <span>{modelLabel}</span>
          {reasoningSupported && <b>{effortLabel}</b>}
          <ChevronDown size={13} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="chat-model-menu chat-model-reasoning-menu"
          side="top"
          sideOffset={9}
          align="end"
          collisionPadding={12}
        >
          <DropdownMenu.Sub>
            <DropdownMenu.SubTrigger className="chat-model-settings-row">
              <strong>模型</strong>
              <span>{modelLabel}</span>
              <ChevronRight size={16} />
            </DropdownMenu.SubTrigger>
            <DropdownMenu.Portal>
              <DropdownMenu.SubContent
                className="chat-model-menu chat-model-submenu"
                sideOffset={8}
                alignOffset={-6}
                collisionPadding={12}
              >
                <DropdownMenu.RadioGroup value={model} onValueChange={onModelChange}>
                  {models.map(item => (
                    <DropdownMenu.RadioItem key={item.id} value={item.id} className="chat-model-option">
                      <span>{item.label}</span>
                      <DropdownMenu.ItemIndicator><Check size={15} /></DropdownMenu.ItemIndicator>
                    </DropdownMenu.RadioItem>
                  ))}
                </DropdownMenu.RadioGroup>
              </DropdownMenu.SubContent>
            </DropdownMenu.Portal>
          </DropdownMenu.Sub>
          {reasoningSupported && (
            <DropdownMenu.Sub>
              <DropdownMenu.SubTrigger className="chat-model-settings-row">
                <strong>推理强度</strong>
                <span>{effortLabel}</span>
                <ChevronRight size={16} />
              </DropdownMenu.SubTrigger>
              <DropdownMenu.Portal>
                <DropdownMenu.SubContent
                  className="chat-model-menu chat-model-submenu chat-reasoning-submenu"
                  sideOffset={8}
                  alignOffset={-6}
                  collisionPadding={12}
                >
                  <DropdownMenu.RadioGroup
                    value={reasoningEffort}
                    onValueChange={value => onReasoningEffortChange(value as ReasoningEffort)}
                  >
                    {REASONING_OPTIONS.filter(option => (
                      option.value === "" || supportedReasoningEfforts.includes(option.value)
                    )).map(option => (
                      <DropdownMenu.RadioItem
                        key={option.value || "auto"}
                        value={option.value}
                        className="chat-reasoning-option"
                      >
                        <span><strong>{option.label}</strong><small>{option.description}</small></span>
                        <DropdownMenu.ItemIndicator><Check size={15} /></DropdownMenu.ItemIndicator>
                      </DropdownMenu.RadioItem>
                    ))}
                  </DropdownMenu.RadioGroup>
                </DropdownMenu.SubContent>
              </DropdownMenu.Portal>
            </DropdownMenu.Sub>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

export function ChatComposer({
  input,
  placeholder,
  disabled,
  active,
  attachments,
  mode,
  approvalMode,
  models,
  model,
  reasoningEffort,
  commandIndex = 0,
  contextControl,
  sendControl,
  canSend,
  textareaRef,
  onInputChange,
  onFiles,
  onRemoveAttachment,
  onSetMode,
  onStartGoal,
  onApprovalModeChange,
  onModelChange,
  onReasoningEffortChange,
  onConfigureModel,
  onCommandSelect,
  onCommandIndexChange,
  onSend,
  attachmentAccept,
}: {
  input: string;
  placeholder: string;
  disabled: boolean;
  active: boolean;
  attachments: ComposerAttachmentView[];
  mode: CollaborationMode;
  approvalMode: ApprovalMode;
  models: ComposerModelOption[];
  model: string;
  reasoningEffort: ReasoningEffort;
  commandIndex?: number;
  contextControl?: ReactNode;
  sendControl?: ReactNode;
  canSend: boolean;
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
  onInputChange: (value: string) => void;
  onFiles: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
  onSetMode: (mode: CollaborationMode) => void;
  onStartGoal: () => void;
  onApprovalModeChange: (mode: ApprovalMode) => void;
  onModelChange: (model: string) => void;
  onReasoningEffortChange: (effort: ReasoningEffort) => void;
  onConfigureModel?: () => void;
  onCommandSelect: (command: ComposerCommand["id"]) => void;
  onCommandIndexChange?: (index: number) => void;
  onSend: () => void;
  attachmentAccept?: string;
}) {
  const internalRef = useRef<HTMLTextAreaElement>(null);
  const resolvedRef = textareaRef || internalRef;
  const commands = visibleComposerCommands(input);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (commands.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      onCommandIndexChange?.((commandIndex + direction + commands.length) % commands.length);
      return;
    }
    if (commands.length && event.key === "Escape") {
      event.preventDefault();
      onInputChange("");
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (commands.length) {
        onCommandSelect(commands[Math.min(commandIndex, commands.length - 1)].id);
        return;
      }
      onSend();
    }
  }

  return (
    <div className="chat-composer" data-ui="sender">
      <ComposerCommandMenu input={input} activeIndex={commandIndex} onSelect={onCommandSelect} />
      {attachments.length > 0 && (
        <div className="chat-attachment-list" aria-label="本轮附件">
          {attachments.map(attachment => (
            <div key={attachment.id} className={`chat-attachment-chip ${attachment.kind}`}>
              {attachment.kind === "image" && attachment.previewUrl
                ? <img src={attachment.previewUrl} alt="" />
                : <span className="chat-attachment-icon"><FileText size={15} /></span>}
              <span className="chat-attachment-copy">
                <strong>{attachment.name}</strong>
                <small>{attachment.kind === "image" ? "图片" : attachment.kind === "text" ? "文本" : "文件"} · {formatAttachmentSize(attachment.size)}</small>
              </span>
              <button type="button" aria-label={`移除附件 ${attachment.name}`} onClick={() => onRemoveAttachment(attachment.id)}>
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      )}
      <textarea
        ref={resolvedRef}
        rows={1}
        value={input}
        onChange={event => onInputChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        aria-label="消息"
        disabled={disabled}
      />
      <div className="chat-composer-footer">
        <ComposerActionMenu
          disabled={disabled}
          active={active}
          onTogglePlan={() => onSetMode(mode === "plan" ? "default" : "plan")}
          onStartGoal={onStartGoal}
          onFiles={onFiles}
          attachmentAccept={attachmentAccept}
        />
        {mode === "plan" && (
          <button className="chat-mode-chip" type="button" title="点击返回默认模式" onClick={() => onSetMode("default")}>
            <ListTodo size={14} />
            <span>计划</span>
          </button>
        )}
        <ApprovalModeMenu value={approvalMode} onChange={onApprovalModeChange} active={active} />
        <span className="chat-composer-spacer" />
        {contextControl}
        <ModelReasoningMenu
          models={models}
          model={model}
          reasoningEffort={reasoningEffort}
          disabled={disabled}
          active={active}
          onModelChange={onModelChange}
          onReasoningEffortChange={onReasoningEffortChange}
          onConfigure={onConfigureModel}
        />
        {sendControl || (
          <button className="chat-send-button" type="button" aria-label="发送消息" title="发送消息" onClick={onSend} disabled={!canSend || disabled}>
            <Send size={15} />
          </button>
        )}
      </div>
    </div>
  );
}
