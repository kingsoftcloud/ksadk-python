import { describe, it, expect } from 'vitest';
import { parseSseChunk, splitSseBuffer } from '../core/transport/sse-parser.js';

describe('splitSseBuffer', () => {
  it('splits on double newline', () => {
    const { chunks, remainder } = splitSseBuffer('event:a\ndata:1\n\nevent:b\ndata:2\n\n');
    expect(chunks).toEqual(['event:a\ndata:1', 'event:b\ndata:2']);
    expect(remainder).toBe('');
  });

  it('keeps incomplete chunk as remainder', () => {
    const { chunks, remainder } = splitSseBuffer('event:a\ndata:1\n\nevent:b\ndata:2');
    expect(chunks).toEqual(['event:a\ndata:1']);
    expect(remainder).toBe('event:b\ndata:2');
  });

  it('handles empty buffer', () => {
    const { chunks, remainder } = splitSseBuffer('');
    expect(chunks).toEqual([]);
    expect(remainder).toBe('');
  });
});

describe('parseSseChunk', () => {
  it('parses event and data', () => {
    const events = parseSseChunk('event: response.output_text.delta\ndata: {"delta":"hello"}');
    expect(events).toHaveLength(1);
    expect(events[0].eventName).toBe('response.output_text.delta');
    expect(events[0].data).toEqual({ delta: 'hello' });
  });

  it('detects [DONE] signal', () => {
    const events = parseSseChunk('data: [DONE]');
    expect(events).toHaveLength(1);
    expect(events[0].eventName).toBe('__done__');
    expect(events[0].data).toBeNull();
  });

  it('handles multiline data', () => {
    const events = parseSseChunk('event: test\ndata: line1\ndata: line2');
    expect(events).toHaveLength(1);
    expect(events[0].eventName).toBe('test');
    expect(events[0].data).toBe('line1\nline2');
  });

  it('skips empty chunks', () => {
    const events = parseSseChunk('');
    expect(events).toHaveLength(0);
  });
});
