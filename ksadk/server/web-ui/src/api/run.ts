import { streamAction } from './client.js';

export async function runAgent(
  body: Record<string, unknown>,
  options?: { signal?: AbortSignal },
): Promise<ReadableStream<Uint8Array>> {
  return streamAction('RunAgent', body, options);
}