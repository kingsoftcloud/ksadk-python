export type ApprovalMode = "ask" | "risk" | "full";

export interface ApprovalModeOption {
  value: ApprovalMode;
  label: string;
  compactLabel: string;
  description: string;
}

export const APPROVAL_MODES: readonly ApprovalModeOption[] = [
  {
    value: "ask",
    label: "请求批准",
    compactLabel: "请求批准",
    description: "文件修改与外部写入会逐次请求确认",
  },
  {
    value: "risk",
    label: "帮我批准",
    compactLabel: "帮我批准",
    description: "仅在检测到风险操作时请求确认",
  },
  {
    value: "full",
    label: "完全访问权限",
    compactLabel: "完全访问",
    description: "不受限制地访问互联网和工作区文件",
  },
] as const;

export function normalizeApprovalMode(value: unknown): ApprovalMode {
  return value === "ask" || value === "risk" || value === "full" ? value : "risk";
}

export function approvalModeStorageKey(agentId: string): string {
  return `agentkit-studio:approval:${agentId}`;
}

export function approvalModeOption(value: ApprovalMode): ApprovalModeOption {
  return APPROVAL_MODES.find(item => item.value === value) || APPROVAL_MODES[1];
}
