import type { StreamProtocol, StreamAction } from './types.js';
import type { TransportEvent } from '../transport/types.js';
import { extractChatCompletionsStreamDelta } from '../../utils/stream-parsing.js';

export class ChatCompletionsProtocol implements StreamProtocol {
  readonly id = 'chat_completions';

  createState(): Record<string, unknown> {
    return {};
  }

  parse(event: TransportEvent): StreamAction[] {
    const data = event.data;
    if (typeof data !== 'object' || data === null) return [];

    const delta = extractChatCompletionsStreamDelta(data as Record<string, unknown>);
    const actions: StreamAction[] = [];

    if (delta.reasoning) {
      actions.push({ type: 'reasoning_delta', text: delta.reasoning });
    }
    if (delta.content) {
      actions.push({ type: 'text_delta', text: delta.content });
    }
    if (delta.finalText) {
      actions.push({ type: 'text_final', text: delta.finalText });
    }

    return actions;
  }
}
