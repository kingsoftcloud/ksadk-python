import type { TransportEvent } from './types.js';

export function parseSseChunk(chunk: string): TransportEvent[] {
  const events: TransportEvent[] = [];
  const trimmed = chunk.trim();
  if (!trimmed) return events;

  let currentEvent = 'message';
  const dataLines: string[] = [];

  for (const line of trimmed.split('\n')) {
    if (line.startsWith('event:')) {
      currentEvent = line.substring(6).trim() || 'message';
    } else if (line.startsWith('data:')) {
      dataLines.push(line.substring(5).trim());
    }
  }

  const dataString = dataLines.join('\n').trim();
  if (dataString === '[DONE]') {
    events.push({ eventName: '__done__', data: null });
    return events;
  }

  if (!dataString) return events;

  try {
    const parsed = JSON.parse(dataString);
    events.push({ eventName: currentEvent, data: parsed });
  } catch {
    events.push({ eventName: currentEvent, data: dataString });
  }

  return events;
}

export function splitSseBuffer(buffer: string): { chunks: string[]; remainder: string } {
  const parts = buffer.split('\n\n');
  const remainder = parts.pop() || '';
  return { chunks: parts, remainder };
}
