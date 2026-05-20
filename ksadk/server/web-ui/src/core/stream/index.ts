import type { StreamProtocol } from './types.js';
import { ResponsesProtocol } from './responses-protocol.js';
import { ChatCompletionsProtocol } from './chat-completions-protocol.js';

const protocols = new Map<string, StreamProtocol>();
protocols.set('responses', new ResponsesProtocol());
protocols.set('chat_completions', new ChatCompletionsProtocol());

export function createProtocol(apiFormat: string): StreamProtocol {
  return protocols.get(apiFormat) || protocols.get('responses')!;
}

export type { StreamProtocol, StreamAction } from './types.js';
