import { useEffect, useState } from "react";
import { authoringStageText, authoringStageTip, formatElapsed } from "../../authoringStages";

/* 对话创建等待期间的阶段化反馈：阶段主文案 + 小贴士 + 已等待时长，
 * 配合文字微光动画（纯 CSS，无新依赖）。codex 模式一轮可能需要 1-3 分钟，
 * 计时与阶段推进共同提供"仍在工作"的信号。阶段名来自
 * GET /api/v1/authoring/conversations:status/{requestId}。 */
export function TextShimmer({
  stage,
  startedAt,
  testId = "authoring-stage-shimmer",
}: {
  stage: string | null | undefined;
  /** 请求开始时的 epoch 毫秒；提供后显示已等待时长。 */
  startedAt?: number | null;
  testId?: string;
}) {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    if (!startedAt || stage === "done" || stage === "failed") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt, stage]);

  const tip = authoringStageTip(stage);
  const elapsed = startedAt ? formatElapsed(Math.max(0, Math.floor((now - startedAt) / 1000))) : null;
  const terminal = stage === "done" || stage === "failed";

  return (
    <span className="authoring-stage" data-testid={testId} data-stage={stage || "unknown"}>
      <span className="text-shimmer">{authoringStageText(stage)}</span>
      {elapsed && !terminal && <span className="authoring-stage-elapsed">已等待 {elapsed}</span>}
      {tip && !terminal && <span className="authoring-stage-tip">{tip}</span>}
    </span>
  );
}
