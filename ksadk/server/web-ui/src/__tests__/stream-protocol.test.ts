import { describe, it, expect } from 'vitest';
import { createProtocol } from '../core/stream/index.js';

describe('StreamProtocol factory', () => {
  it('returns ResponsesProtocol for "responses"', () => {
    const protocol = createProtocol('responses');
    expect(protocol.id).toBe('responses');
  });

  it('returns ChatCompletionsProtocol for "chat_completions"', () => {
    const protocol = createProtocol('chat_completions');
    expect(protocol.id).toBe('chat_completions');
  });

  it('defaults to responses for unknown format', () => {
    const protocol = createProtocol('unknown');
    expect(protocol.id).toBe('responses');
  });

  it('createState returns object', () => {
    const protocol = createProtocol('responses');
    const state = protocol.createState();
    expect(typeof state).toBe('object');
  });
});
