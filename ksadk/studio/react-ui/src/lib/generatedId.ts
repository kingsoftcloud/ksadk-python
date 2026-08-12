export function generateAgentSlug(): string {
  const bytes = new Uint8Array(4);
  crypto.getRandomValues(bytes);
  const suffix = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return `agentkit-${suffix}`;
}
