import type { TransportEvent } from './types.js';

export function parseSseChunk(chunk: string): TransportEvent[] {
  const events: TransportEvent[] = [];
  const trimmed = chunk.trim();
  if (!trimmed) return events;

  // SSE comment frames (e.g. ": ping") — keepalive from server
  const allLines = trimmed.split('\n');
  if (allLines.every(l => l.startsWith(':'))) {
    events.push({ eventName: '__ping__', data: null });
    return events;
  }

  let currentEvent = 'message';
  const dataLines: string[] = [];

  for (const line of allLines) {
    if (line.startsWith(':')) continue; // skip comment lines
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
