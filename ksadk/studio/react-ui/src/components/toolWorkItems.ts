import type { ToolDisplay, ToolExecution } from "./ToolExecutionCard";

export type WorkBlock = {
  id: string; type: string; status: string; toolName?: string;
  extra?: Record<string, unknown>;
};
export type ToolWorkItem = {
  family: "explore" | "terminal" | null;
  tools: ToolExecution[];
  /** A stage can continue while its latest tool has already completed. */
  running: boolean;
};

export function executionFromBlock(block: WorkBlock): ToolExecution | undefined {
  if (block.type !== "tool" || block.extra?.approvalRequestId) return undefined;
  const supplied = block.extra?.toolDisplay as ToolDisplay | undefined;
  // Historical or third-party calls may lack a public receipt. Keep them on
  // the same execution surface without reconstructing private args/output or
  // falling back to the legacy tool row. Approvals remain shared above.
  const display: ToolDisplay = supplied?.version === "tool-display/v1" ? supplied
    : { version: "tool-display/v1", category: "tool" };
  return { id: block.id, callId: String(block.extra?.callId || block.extra?.call_id || block.id),
    name: block.toolName, display, status: block.status === "error" ? "failed" : block.status };
}

function familyOf(tool?: ToolExecution): ToolWorkItem["family"] {
  if (tool?.display.category === "read" || tool?.display.category === "search") return "explore";
  if (tool?.display.category === "terminal") return "terminal";
  return null;
}

/**
 * ZCode aac4755 conversationAssistantWorkItems.ts: a second contiguous tool
 * creates a group, anchored to the first real call. Text, authorized thinking,
 * approvals and other tools end it. Changes remain individual as in ZCode.
 * This is a view index: canonical blocks, receipts and call IDs are untouched.
 */
export function buildToolWorkItems(blocks: readonly WorkBlock[], streaming: boolean) {
  const first = new Map<string, ToolWorkItem>();
  const groupedChildren = new Set<string>();
  for (let index = 0; index < blocks.length;) {
    const block = blocks[index];
    const tool = executionFromBlock(block);
    if (!tool) { index++; continue; }
    const family = familyOf(tool);
    const tools = [tool];
    index++;
    if (family) {
      while (index < blocks.length) {
        const next = executionFromBlock(blocks[index]);
        if (!next || familyOf(next) !== family) break;
        tools.push(next); groupedChildren.add(next.id); index++;
      }
    }
    first.set(block.id, { family: tools.length > 1 ? family : null, tools,
      running: streaming && index === blocks.length });
  }
  return { first, groupedChildren };
}

export function exploreCounts(tools: readonly ToolExecution[]) {
  const counts = { search: 0, list: 0, file: 0 };
  for (const tool of tools) {
    if (tool.display.category === "search") counts.search++;
    else if (tool.name === "list_workspace_files") counts.list++;
    else counts.file++;
  }
  return [counts.search && `${counts.search} 搜索`, counts.list && `${counts.list} 列表`,
    counts.file && `${counts.file} 文件`].filter(Boolean).join("，");
}
