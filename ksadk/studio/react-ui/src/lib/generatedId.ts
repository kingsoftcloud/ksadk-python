export function generateAgentSlug(): string {
  const ts = Date.now().toString(36).slice(-6);
  const rand = Math.random().toString(36).slice(2, 6);
  return `agent-${ts}${rand}`;
}
