export interface CloudDeploymentSummary {
  id: string;
  agentId?: string;
  status?: string;
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
 * 同一 Agent 的重试、更新和回滚可以留下多张 receipt；对话只依赖 AgentId，
 * 因此保留状态最可用的一张。相同状态时保留 API 的第一张，刷新选择稳定。
 */
export function selectCloudChatDeployments(
  items: CloudDeploymentSummary[],
): CloudDeploymentSummary[] {
  const selected = new Map<string, CloudDeploymentSummary>();
  for (const item of items) {
    const agentId = item.agentId?.trim();
    if (!agentId) continue;
    const current = selected.get(agentId);
    const nextPriority = STATUS_PRIORITY[item.status || ""] || 0;
    const currentPriority = STATUS_PRIORITY[current?.status || ""] || 0;
    if (!current || nextPriority > currentPriority) selected.set(agentId, item);
  }
  return [...selected.values()];
}
