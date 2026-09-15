// Shared channel control-plane helpers.
// 契约: agentengine-channel-api-contract.md
// 控制面 Base path: /agentengine/api/v1  全部 POST + JSON body
// 请求头: X-Ksc-Account-Id (必填)
// 统一响应: { Code, Message, RequestId, Action, Data }  Code=0 表示成功

import { apiFetch } from "../api";

export const CHANNEL_API_BASE = "/agentengine/api/v1";
// 测试租户 ID，正式环境应从 Studio session 获取
export const CHANNEL_ACCOUNT_ID = "2000003485";

export interface ChannelEnvelope<T> {
  Code: number;
  Message: string;
  RequestId: string;
  Action: string;
  Data: T;
}

export interface ListResult<T> {
  Items: T[];
  Total: number;
}

export async function channelApi<T>(action: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await apiFetch(`${CHANNEL_API_BASE}/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Ksc-Account-Id": CHANNEL_ACCOUNT_ID,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const errorBody = await response.json();
      message = errorBody.Message || errorBody.message || message;
    } catch { /* keep HTTP status fallback */ }
    throw new Error(message);
  }
  const payload: ChannelEnvelope<T> = await response.json();
  if (payload.Code !== 0) {
    throw new Error(payload.Message || `错误码 ${payload.Code}`);
  }
  return payload.Data;
}

// ── Types (PascalCase 对齐契约) ──────────────────────────────────────────────

export type ChannelType = "wps-xiezuo" | "feishu" | "wecom";
export type DmPolicy = "pairing" | "open";
export type GroupPolicy = "allowlist" | "open";
export type SessionScope = "per-peer" | "per-group";

export interface Channel {
  Id: string;
  AgentId: string;
  Channel: ChannelType;
  ChannelAccountId: string;
  Enabled: boolean;
  DmPolicy: DmPolicy;
  GroupPolicy: GroupPolicy;
  RequireMention: boolean;
  SessionScope: SessionScope;
  SecretRef: string;
  ConfigJson: string;
  CreatedBy: string;
  CreatedAt: string;
  UpdatedAt: string;
}

// ── Labels & options ────────────────────────────────────────────────────────

export const CHANNEL_LABELS: Record<ChannelType, string> = {
  "wps-xiezuo": "WPS 协作",
  feishu: "飞书",
  wecom: "企业微信",
};

export const CHANNEL_OPTIONS = (Object.keys(CHANNEL_LABELS) as ChannelType[]).map(value => ({
  value,
  label: CHANNEL_LABELS[value],
}));

export const PLATFORM_BADGE: Record<ChannelType, string> = {
  "wps-xiezuo": "WPS",
  feishu: "飞书",
  wecom: "企微",
};

export const DM_POLICY_LABELS: Record<DmPolicy, string> = {
  pairing: "需配对",
  open: "开放",
};

export const DM_POLICY_OPTIONS = (Object.keys(DM_POLICY_LABELS) as DmPolicy[]).map(value => ({
  value,
  label: DM_POLICY_LABELS[value],
}));

export const GROUP_POLICY_LABELS: Record<GroupPolicy, string> = {
  allowlist: "白名单",
  open: "开放",
};

export const GROUP_POLICY_OPTIONS = (Object.keys(GROUP_POLICY_LABELS) as GroupPolicy[]).map(value => ({
  value,
  label: GROUP_POLICY_LABELS[value],
}));

export const SESSION_SCOPE_LABELS: Record<SessionScope, string> = {
  "per-peer": "按用户",
  "per-group": "按群聊",
};

export const SESSION_SCOPE_OPTIONS = (Object.keys(SESSION_SCOPE_LABELS) as SessionScope[]).map(value => ({
  value,
  label: SESSION_SCOPE_LABELS[value],
}));

// ── Helpers ─────────────────────────────────────────────────────────────────

export function formatChannelDate(iso: string): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
