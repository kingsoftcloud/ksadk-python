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
        id: 'evt-reasoning',
        role: 'model',
        content: '',
        reasoning: '先分析问题',
        eventType: 'reasoning',
      },
      {
        id: 'evt-assistant',
        role: 'model',
        content: '你好，很高兴见到你。',
        reasoning: undefined,
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
