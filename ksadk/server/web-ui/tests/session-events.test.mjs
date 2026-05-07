import test from 'node:test';
import assert from 'node:assert/strict';

async function loadSessionEventUtils() {
  return import('../src/utils/session-events.js').catch(() => null);
}

test('session event utils restore persisted reasoning events', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-user',
      EventType: 'user_message',
      Content: { role: 'user', parts: [{ text: '你好' }] },
      Timestamp: 1,
    },
    {
      EventId: 'evt-reasoning',
      EventType: 'reasoning',
      Content: { role: 'model', parts: [{ text: '先分析问题' }] },
      Timestamp: 2,
    },
    {
      EventId: 'evt-assistant',
      EventType: 'assistant_message',
      Content: { role: 'model', parts: [{ text: '你好，很高兴见到你。' }] },
      Timestamp: 3,
    },
    {
      EventId: 'evt-status-start',
      EventType: 'run_status',
      InvocationId: 'inv-1',
      Content: { status: 'in_progress' },
      Timestamp: 4,
    },
    {
      EventId: 'evt-status-done',
      EventType: 'run_status',
      InvocationId: 'inv-1',
      Content: { status: 'completed' },
      Timestamp: 5,
    },
  ]);

  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      reasoning: message.reasoning,
      eventType: message.eventType,
    })),
    [
      {
        id: 'evt-user',
        role: 'user',
        content: '你好',
        reasoning: undefined,
        eventType: 'user_message',
      },
      {
        id: 'evt-assistant',
        role: 'model',
        content: '你好，很高兴见到你。',
        reasoning: '先分析问题',
        eventType: 'assistant_message',
      },
    ],
  );
});

test('session event utils suppress stale running banners after assistant output exists', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-running',
      EventType: 'run_status',
      InvocationId: 'inv-legacy',
      Content: { status: 'in_progress' },
      Timestamp: 1,
    },
    {
      EventId: 'evt-assistant',
      EventType: 'assistant_message',
      InvocationId: 'inv-legacy',
      Content: { role: 'model', parts: [{ text: '已经回复了' }] },
      Timestamp: 2,
    },
  ]);

  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      eventType: message.eventType,
    })),
    [
      {
        id: 'evt-assistant',
        role: 'model',
        content: '已经回复了',
        eventType: 'assistant_message',
      },
    ],
  );
});

test('session event utils preserve assistant identifiers for feedback binding', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-assistant',
      EventType: 'assistant_message',
      Content: { role: 'model', parts: [{ text: '已经回复了' }] },
      Metadata: {
        response_id: 'resp-123',
        trace_id: 'trace-123',
        root_span_id: 'span-123',
      },
      Timestamp: 1,
    },
  ]);

  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      eventId: message.eventId,
      responseId: message.responseId,
      traceId: message.traceId,
      rootSpanId: message.rootSpanId,
    })),
    [
      {
        id: 'evt-assistant',
        eventId: 'evt-assistant',
        responseId: 'resp-123',
        traceId: 'trace-123',
        rootSpanId: 'span-123',
      },
    ],
  );
});

test('session event utils restore completed run with persisted reasoning before assistant output', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-user',
      EventType: 'user_message',
      InvocationId: 'inv-pre',
      Content: { role: 'user', parts: [{ text: '我只发了一次' }] },
      Timestamp: 1,
    },
    {
      EventId: 'evt-running',
      EventType: 'run_status',
      InvocationId: 'inv-pre',
      Content: { status: 'in_progress' },
      Timestamp: 2,
    },
    {
      EventId: 'evt-reasoning-1',
      EventType: 'reasoning',
      InvocationId: 'inv-pre',
      Content: { role: 'model', parts: [{ text: '检查历史消息。' }] },
      Timestamp: 3,
    },
    {
      EventId: 'evt-reasoning-2',
      EventType: 'reasoning',
      InvocationId: 'inv-pre',
      Content: { role: 'model', parts: [{ text: '确认当前用户输入只出现一次。' }] },
      Timestamp: 4,
    },
    {
      EventId: 'evt-assistant',
      EventType: 'assistant_message',
      InvocationId: 'inv-pre',
      Content: {
        role: 'model',
        parts: [{ text: '好的，我确认只收到了您这一次发送。' }],
      },
      Timestamp: 5,
    },
    {
      EventId: 'evt-completed',
      EventType: 'run_status',
      InvocationId: 'inv-pre',
      Content: { status: 'completed' },
      Timestamp: 6,
    },
  ]);

  assert.equal(
    messages.some((message) => message.status === 'running'),
    false,
    'completed runs with persisted output must not show a stale running banner',
  );
  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      reasoning: message.reasoning,
      eventType: message.eventType,
    })),
    [
      {
        id: 'evt-user',
        role: 'user',
        content: '我只发了一次',
        reasoning: undefined,
        eventType: 'user_message',
      },
      {
        id: 'evt-assistant',
        role: 'model',
        content: '好的，我确认只收到了您这一次发送。',
        reasoning: '检查历史消息。确认当前用户输入只出现一次。',
        eventType: 'assistant_message',
      },
    ],
  );
});

test('session event utils coalesce reasoning deltas for the same invocation', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-user',
      EventType: 'user_message',
      InvocationId: 'inv-delta',
      Content: { role: 'user', parts: [{ text: '说一句话' }] },
      Timestamp: 1,
    },
    {
      EventId: 'evt-running',
      EventType: 'run_status',
      InvocationId: 'inv-delta',
      Content: { status: 'in_progress' },
      Timestamp: 2,
    },
    {
      EventId: 'evt-reasoning-1',
      EventType: 'reasoning',
      InvocationId: 'inv-delta',
      Content: { role: 'model', parts: [{ text: '先' }] },
      Timestamp: 3,
    },
    {
      EventId: 'evt-reasoning-2',
      EventType: 'reasoning',
      InvocationId: 'inv-delta',
      Content: { role: 'model', parts: [{ text: '分析' }] },
      Timestamp: 4,
    },
    {
      EventId: 'evt-reasoning-3',
      EventType: 'reasoning',
      InvocationId: 'inv-delta',
      Content: { role: 'model', parts: [{ text: '。' }] },
      Timestamp: 5,
    },
    {
      EventId: 'evt-assistant',
      EventType: 'assistant_message',
      InvocationId: 'inv-delta',
      Content: { role: 'model', parts: [{ text: '完成。' }] },
      Timestamp: 6,
    },
    {
      EventId: 'evt-completed',
      EventType: 'run_status',
      InvocationId: 'inv-delta',
      Content: { status: 'completed' },
      Timestamp: 7,
    },
  ]);

  const reasoningMessages = messages.filter((message) => message.reasoning);
  assert.equal(reasoningMessages.length, 1);
  assert.equal(reasoningMessages[0].id, 'evt-assistant');
  assert.equal(reasoningMessages[0].reasoning, '先分析。');
  assert.deepEqual(
    messages.map((message) => ({
      role: message.role,
      content: message.content,
      reasoning: message.reasoning,
      eventType: message.eventType,
    })),
    [
      {
        role: 'user',
        content: '说一句话',
        reasoning: undefined,
        eventType: 'user_message',
      },
      {
        role: 'model',
        content: '完成。',
        reasoning: '先分析。',
        eventType: 'assistant_message',
      },
    ],
  );
});

test('session event utils do not duplicate persisted and response-output reasoning', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-user',
      EventType: 'user_message',
      InvocationId: 'inv-reasoning-output',
      Content: { role: 'user', parts: [{ text: '写一个python快排的示例' }] },
      Timestamp: 1,
    },
    {
      EventId: 'evt-reasoning',
      EventType: 'reasoning',
      InvocationId: 'inv-reasoning-output',
      Content: { role: 'model', parts: [{ text: '先分析问题' }] },
      Timestamp: 2,
    },
    {
      EventId: 'evt-assistant',
      EventType: 'assistant_message',
      InvocationId: 'inv-reasoning-output',
      Content: { role: 'model', parts: [{ text: '最终答案' }] },
      Metadata: {
        responses_output: [
          {
            id: 'rs_1',
            type: 'reasoning',
            summary: [{ text: '先分析问题' }],
          },
          {
            id: 'msg_1',
            type: 'message',
            content: [{ type: 'output_text', text: '最终答案' }],
          },
        ],
      },
      Timestamp: 3,
    },
  ]);

  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      reasoning: message.reasoning,
      eventType: message.eventType,
    })),
    [
      {
        id: 'evt-user',
        role: 'user',
        content: '写一个python快排的示例',
        reasoning: undefined,
        eventType: 'user_message',
      },
      {
        id: 'evt-assistant',
        role: 'model',
        content: '最终答案',
        reasoning: '先分析问题',
        eventType: 'assistant_message',
      },
    ],
  );
});

test('session event utils dedupe duplicate assistant output for the same invocation', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-user',
      EventType: 'user_message',
      InvocationId: 'inv-dup',
      Content: { role: 'user', parts: [{ text: '你好' }] },
      Timestamp: 1,
    },
    {
      EventId: 'evt-assistant-1',
      EventType: 'assistant_message',
      InvocationId: 'inv-dup',
      Content: { role: 'model', parts: [{ text: '你好，我在。' }] },
      Metadata: { response_id: 'resp-dup' },
      Timestamp: 2,
    },
    {
      EventId: 'evt-assistant-2',
      EventType: 'assistant_message',
      InvocationId: 'inv-dup',
      Content: { role: 'model', parts: [{ text: '你好，我在。' }] },
      Metadata: { response_id: 'resp-dup' },
      Timestamp: 3,
    },
    {
      EventId: 'evt-completed',
      EventType: 'run_status',
      InvocationId: 'inv-dup',
      Content: { status: 'completed' },
      Timestamp: 4,
    },
  ]);

  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
    })),
    [
      { id: 'evt-user', role: 'user', content: '你好' },
      { id: 'evt-assistant-1', role: 'model', content: '你好，我在。' },
    ],
  );
});

test('session event utils dedupe same assistant output even when response ids differ', async () => {
  const sessionEvents = await loadSessionEventUtils();

  assert.ok(sessionEvents, 'expected session event helpers to exist');
  const messages = sessionEvents.buildMessagesFromSessionEvents([
    {
      EventId: 'evt-user',
      EventType: 'user_message',
      InvocationId: 'inv-resp-dup',
      Content: { role: 'user', parts: [{ text: '你好' }] },
      Timestamp: 1,
    },
    {
      EventId: 'evt-assistant-1',
      EventType: 'assistant_message',
      InvocationId: 'inv-resp-dup',
      Content: { role: 'model', parts: [{ text: '你好，我在。' }] },
      Metadata: { response_id: 'resp-first' },
      Timestamp: 2,
    },
    {
      EventId: 'evt-assistant-2',
      EventType: 'assistant_message',
      InvocationId: 'inv-resp-dup',
      Content: { role: 'model', parts: [{ text: '你好，我在。' }] },
      Metadata: { response_id: 'resp-second' },
      Timestamp: 3,
    },
  ]);

  assert.deepEqual(
    messages.map((message) => ({
      id: message.id,
      responseId: message.responseId,
      role: message.role,
      content: message.content,
    })),
    [
      { id: 'evt-user', responseId: undefined, role: 'user', content: '你好' },
      {
        id: 'evt-assistant-1',
        responseId: 'resp-first',
        role: 'model',
        content: '你好，我在。',
      },
    ],
  );
});
