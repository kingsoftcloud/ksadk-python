export function generateAgentSlug(randomBytes?: () => Uint8Array): string {
  const bytes = randomBytes ? randomBytes() : new Uint8Array(4);
  if (!randomBytes && globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else if (!randomBytes) {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  const suffix = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return `agentkit-${suffix}`;
}
