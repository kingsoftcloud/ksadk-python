/** Short, neutral prompts shown before the first turn of a Studio conversation. */
export const STUDIO_WELCOME_COPY = [
  "今天想一起完成什么？",
  "把想法交给我，我们开始吧。",
  "准备好了，告诉我你的目标。",
  "从一个问题开始，就能走得更远。",
  "需要我帮你整理哪件事？",
  "让我们把下一步变得清晰。",
] as const;

export function pickStudioWelcome(random: () => number = Math.random): string {
  const index = Math.min(
    STUDIO_WELCOME_COPY.length - 1,
    Math.max(0, Math.floor(random() * STUDIO_WELCOME_COPY.length)),
  );
  return STUDIO_WELCOME_COPY[index];
}
