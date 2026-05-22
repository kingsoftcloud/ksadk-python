import type { RuntimeTransport, TransportCallbacks } from './types.js';
import { streamAction, streamGetAction } from '../../api/client.js';
import { parseSseChunk, splitSseBuffer } from './sse-parser.js';
import { useStreamingStore } from '../../stores/streaming.js';

function handleTransportEvent(event: import('./types.js').TransportEvent, callbacks: TransportCallbacks) {
  if (event.eventName === '__ping__') {
    useStreamingStore.getState().updateActivity({ countEvent: false });
    return;
  }
  callbacks.onEvent(event);
}

export class SsePostTransport implements RuntimeTransport {
  readonly protocol = 'sse-post';

  async connect(
    options: {
      action: string;
      method?: 'POST' | 'GET';
      body?: Record<string, unknown>;
      params?: Record<string, string>;
      signal?: AbortSignal;
    },
    callbacks: TransportCallbacks,
  ): Promise<() => void> {
    const stream = await streamAction(options.action, options.body || {}, { signal: options.signal });
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let aborted = false;

    const readLoop = async () => {
      try {
        while (!aborted) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const { chunks, remainder } = splitSseBuffer(buffer);
          buffer = remainder;

          for (const chunk of chunks) {
            if (!chunk.trim()) continue;
            const events = parseSseChunk(chunk);
            for (const event of events) {
              if (event.eventName === '__done__') {
                callbacks.onComplete();
                return;
              }
              handleTransportEvent(event, callbacks);
            }
          }
        }
        callbacks.onComplete();
      } catch (error) {
        if (!aborted) {
          callbacks.onError(error instanceof Error ? error : new Error(String(error)));
        }
      }
    };

    void readLoop();

    return () => {
      aborted = true;
      reader.cancel().catch(() => {});
    };
  }
}

export class SseGetTransport implements RuntimeTransport {
  readonly protocol = 'sse-get';

  async connect(
    options: {
      action: string;
      method?: 'POST' | 'GET';
      body?: Record<string, unknown>;
      params?: Record<string, string>;
      signal?: AbortSignal;
    },
    callbacks: TransportCallbacks,
  ): Promise<() => void> {
    const stream = await streamGetAction(options.action, options.params || {}, { signal: options.signal });
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let aborted = false;

    const readLoop = async () => {
      try {
        while (!aborted) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const { chunks, remainder } = splitSseBuffer(buffer);
          buffer = remainder;

          for (const chunk of chunks) {
            if (!chunk.trim()) continue;
            const events = parseSseChunk(chunk);
            for (const event of events) {
              if (event.eventName === '__done__') {
                callbacks.onComplete();
                return;
              }
              handleTransportEvent(event, callbacks);
            }
          }
        }
        callbacks.onComplete();
      } catch (error) {
        if (!aborted) {
          callbacks.onError(error instanceof Error ? error : new Error(String(error)));
        }
      }
    };

    void readLoop();

    return () => {
      aborted = true;
      reader.cancel().catch(() => {});
    };
  }
}
