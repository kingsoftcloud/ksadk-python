/* 对话创建 Agent 的阶段化文案：阶段名由后端
 * GET /api/v1/authoring/conversations:status/{requestId} 提供。
 * 只做两段式文案，不解析 Draft Patch 内容。 */

export type AuthoringStage =
  | "resolving_model"
  | "generating"
  | "validating"
  | "correcting"
  | "done"
  | "failed";

export const AUTHORING_STAGE_TEXT: Record<string, string> = {
  resolving_model: "正在理解你的需求…",
  generating: "正在理解你的需求…",
  validating: "正在生成 Agent 配置…",
  correcting: "正在生成 Agent 配置…",
  done: "方案生成完成",
  failed: "生成失败",
};

export function authoringStageText(stage: string | null | undefined): string {
  return (stage && AUTHORING_STAGE_TEXT[stage]) || AUTHORING_STAGE_TEXT.resolving_model;
}
