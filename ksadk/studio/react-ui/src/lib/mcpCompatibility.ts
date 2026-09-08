/** Match the current backend runtime contract before offering an MCP binding. */
export function mcpUnavailableReason(item: {
  contract?: { materialization?: string; discoveredTools?: unknown[] };
  health?: { toolCount?: number };
}, runtime: string): string {
  if (item.contract?.materialization !== 'dsh-profile') return '';
  if (runtime !== 'harness') return '当前 Runtime 尚未接入 DSH Profile MCP；插件设置和渠道服务无需绑定此项。';
  const count = item.health?.toolCount ?? item.contract.discoveredTools?.length;
  return count === 0 ? '此 Profile 当前没有可绑定的 MCP 工具。' : '';
}
