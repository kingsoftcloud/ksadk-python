export interface CloudDeploymentSummary {
  id: string;
  agentId?: string;
  status?: string;
  agentName?: string;
  endpoint?: string;
  framework?: string;
  runtimeType?: string;
  capabilities?: Record<string, unknown>;
  chatTransport?: CloudChatRouteKind;
  chatRoutingReason?: CloudChatRoutingReason;
  versionId?: string;
  updatedAt?: string;
  source?: "receipt" | "account";
}

export interface AccountCloudAgentSummary {
  agentId?: string;
  name?: string;
  status?: string;
  endpoint?: string;
  framework?: string;
  runtimeType?: string;
  capabilities?: Record<string, unknown>;
  chatTransport?: CloudChatRouteKind;
  chatRoutingReason?: CloudChatRoutingReason;
  versionId?: string;
  updatedAt?: string;
}

export type CloudChatRouteKind = "studio-session-events" | "official-dashboard";
export type CloudChatRoutingReason =
  | "declared-session-event-chat-capability"
  | "native-runtime-without-session-event-chat-capability"
  | "studio-compatible-framework";

export interface CloudChatRoute {
  kind: CloudChatRouteKind;
  reason: CloudChatRoutingReason;
}

const NATIVE_DASHBOARD_FRAMEWORKS = new Set(["hermes", "openclaw"]);

function declaredSessionEventChatCapability(
  capabilities: Record<string, unknown> | undefined,
): boolean {
  if (!capabilities) return false;
  const declaration = capabilities.sessionEventChat
    ?? capabilities.SessionEventChat
    ?? capabilities.session_event_chat;
  if (declaration === true) return true;
  if (!declaration || typeof declaration !== "object") return false;
  const capability = declaration as Record<string, unknown>;
  return capability.enabled === true
    || capability.Enabled === true
    || capability.supported === true
    || capability.Supported === true;
}

export function resolveCloudChatRoute(
  target: Pick<CloudDeploymentSummary,
    "framework" | "runtimeType" | "capabilities" | "chatTransport" | "chatRoutingReason">,
): CloudChatRoute {
  if (target.chatTransport) {
    return {
      kind: target.chatTransport,
      reason: target.chatRoutingReason || (target.chatTransport === "official-dashboard"
        ? "native-runtime-without-session-event-chat-capability"
        : "studio-compatible-framework"),
    };
  }
  if (declaredSessionEventChatCapability(target.capabilities)) {
    return {
      kind: "studio-session-events",
      reason: "declared-session-event-chat-capability",
    };
  }
  const runtimeType = String(target.runtimeType || target.framework || "").trim().toLowerCase();
  if (NATIVE_DASHBOARD_FRAMEWORKS.has(runtimeType)) {
    return {
      kind: "official-dashboard",
      reason: "native-runtime-without-session-event-chat-capability",
    };
  }
  return {
    kind: "studio-session-events",
    reason: "studio-compatible-framework",
  };
}

const STATUS_PRIORITY: Record<string, number> = {
  READY: 5,
  DEPLOYING: 4,
  ADMITTING: 3,
  FAILED: 2,
  ROLLED_BACK: 1,
};

/**
 * 会话目标按云端 Agent 去重，而不是按本地 deployment receipt 展开。
 * 同一 Agent 的重试、更新和回滚可以留下多张 receipt；优先选择与云端
 * 当前版本匹配的 receipt，再按状态可用性兜底。相同条件下保留 API 的
 * 第一张，避免刷新时无意义地抖动选择。
 */
export function selectCloudChatDeployments(
  items: CloudDeploymentSummary[],
  preferredVersionsByAgent: ReadonlyMap<string, string> = new Map(),
): CloudDeploymentSummary[] {
  const selected = new Map<string, CloudDeploymentSummary>();
  for (const item of items) {
    const agentId = item.agentId?.trim();
    if (!agentId) continue;
    const current = selected.get(agentId);
    const preferredVersion = preferredVersionsByAgent.get(agentId)?.trim();
    const nextMatchesLiveVersion = Boolean(preferredVersion && item.versionId === preferredVersion);
    const currentMatchesLiveVersion = Boolean(preferredVersion && current?.versionId === preferredVersion);
    const nextPriority = STATUS_PRIORITY[item.status || ""] || 0;
    const currentPriority = STATUS_PRIORITY[current?.status || ""] || 0;
    if (
      !current
      || (nextMatchesLiveVersion && !currentMatchesLiveVersion)
      || (nextMatchesLiveVersion === currentMatchesLiveVersion && nextPriority > currentPriority)
    ) selected.set(agentId, item);
  }
  return [...selected.values()];
}

export function mergeCloudChatTargets(
  receipts: CloudDeploymentSummary[],
  accountAgents: AccountCloudAgentSummary[],
): CloudDeploymentSummary[] {
  const accountByAgentId = new Map<string, AccountCloudAgentSummary>();
  for (const item of accountAgents) {
    const agentId = item.agentId?.trim();
    if (agentId && !accountByAgentId.has(agentId)) accountByAgentId.set(agentId, item);
  }
  const preferredVersionsByAgent = new Map(
    [...accountByAgentId.entries()].flatMap(([agentId, item]) => (
      item.versionId?.trim() ? [[agentId, item.versionId.trim()] as const] : []
    )),
  );
  const selectedReceipts = selectCloudChatDeployments(receipts, preferredVersionsByAgent).map(item => {
    const account = accountByAgentId.get(item.agentId?.trim() || "");
    return {
      ...item,
      agentName: account?.name || item.agentName,
      // ListAgents is the live control-plane projection; receipts retain identity
      // and provenance, while account discovery supplies fresher mutable facts.
      status: account?.status || item.status,
      endpoint: account?.endpoint || item.endpoint,
      framework: account?.framework || item.framework,
      runtimeType: account?.runtimeType || item.runtimeType,
      capabilities: account?.capabilities || item.capabilities,
      chatTransport: account?.chatTransport || item.chatTransport,
      chatRoutingReason: account?.chatRoutingReason || item.chatRoutingReason,
      versionId: account?.versionId || item.versionId,
      updatedAt: account?.updatedAt || item.updatedAt,
      source: "receipt" as const,
    };
  });
  const receiptAgentIds = new Set(selectedReceipts.map(item => item.agentId));
  const accountOnly = [...accountByAgentId.values()].flatMap(item => {
    const agentId = item.agentId?.trim();
    if (!agentId || receiptAgentIds.has(agentId)) return [];
    return [{
      id: `account:${agentId}`,
      agentId,
      agentName: item.name || agentId,
      status: item.status,
      endpoint: item.endpoint,
      framework: item.framework,
      runtimeType: item.runtimeType,
      capabilities: item.capabilities,
      chatTransport: item.chatTransport,
      chatRoutingReason: item.chatRoutingReason,
      versionId: item.versionId,
      updatedAt: item.updatedAt,
      source: "account" as const,
    }];
  });
  return [...selectedReceipts, ...accountOnly];
}
