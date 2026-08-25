/* 对话创建 Agent 的阶段化文案：阶段名由后端
 * GET /api/v1/authoring/conversations:status/{requestId} 提供。
 * codex 模式耗时较长（模型在沙箱内编写配置文件），文案需要给用户
 * 持续的"正在推进"感：阶段推进 + 已等待时长 + 阶段小贴士。 */

export type AuthoringStage =
  | "resolving_model"
  | "generating"
  | "codex_writing"
  | "validating"
  | "correcting"
  | "done"
  | "failed";

interface StageCopy {
  text: string;
  tip: string;
}

export const AUTHORING_STAGE_COPY: Record<string, StageCopy> = {
  resolving_model: {
    text: "正在理解你的需求…",
    tip: "分析对话内容，确定 Agent 的框架与能力",
  },
  generating: {
    text: "正在理解你的需求…",
    tip: "分析对话内容，确定 Agent 的框架与能力",
  },
  codex_writing: {
    text: "Codex 正在编写 Agent 配置…",
    tip: "AI 编程助手正在沙箱中编写 agentkit.yaml，通常需要 1-3 分钟，质量优先",
  },
  validating: {
    text: "正在校验配置…",
    tip: "检查配置完整性：名称、模型、运行时与提示词",
  },
  correcting: {
    text: "正在修正配置…",
    tip: "发现少量问题，正在按校验意见修正后重新生成",
  },
  done: {
    text: "方案生成完成",
    tip: "",
  },
  failed: {
    text: "生成失败",
    tip: "",
  },
};

export function authoringStageText(stage: string | null | undefined): string {
  return (
    (stage && AUTHORING_STAGE_COPY[stage]?.text) || AUTHORING_STAGE_COPY.resolving_model.text
  );
}

export function authoringStageTip(stage: string | null | undefined): string {
  return (stage && AUTHORING_STAGE_COPY[stage]?.tip) || AUTHORING_STAGE_COPY.resolving_model.tip;
}

export function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m${s}s` : `${m}m`;
}
