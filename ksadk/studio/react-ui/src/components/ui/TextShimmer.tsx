import { authoringStageText } from "../../authoringStages";

/* 对话创建等待期间的阶段化反馈：两段式文案 + 文字微光动画（纯 CSS，无新依赖）。
 * 阶段名来自 GET /api/v1/authoring/conversations:status/{requestId}。 */
export function TextShimmer({
  stage,
  testId = "authoring-stage-shimmer",
}: {
  stage: string | null | undefined;
  testId?: string;
}) {
  return (
    <span className="text-shimmer" data-testid={testId} data-stage={stage || "unknown"}>
      {authoringStageText(stage)}
    </span>
  );
}
