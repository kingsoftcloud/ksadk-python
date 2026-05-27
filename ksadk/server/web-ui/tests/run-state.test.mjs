import test from 'node:test';
import assert from 'node:assert/strict';

async function loadRunStateUtils() {
  return import('../src/utils/run-state.js').catch(() => null);
}

test('run state utils identify active invocations from replayed events', async () => {
  const runStateUtils = await loadRunStateUtils();

  assert.ok(runStateUtils, 'expected run state helpers to exist');
  assert.deepEqual(
    runStateUtils.findActiveRunIds([
      { EventType: 'run_status', InvocationId: 'inv-done', Content: { status: 'in_progress' } },
      { EventType: 'run_status', InvocationId: 'inv-live', Content: { status: 'in_progress' } },
      { EventType: 'run_status', InvocationId: 'inv-done', Content: { status: 'completed' } },
    ]),
    ['inv-live'],
  );
});

test('run state utils keep in-progress invocations active after persisted output', async () => {
  const runStateUtils = await loadRunStateUtils();

  assert.ok(runStateUtils, 'expected run state helpers to exist');
  assert.deepEqual(
    runStateUtils.findActiveRunIds([
      { EventType: 'run_status', InvocationId: 'inv-replied', Content: { status: 'in_progress' } },
      {
        EventType: 'reasoning',
        InvocationId: 'inv-replied',
        Content: { text: 'thinking...' },
      },
      {
        EventType: 'tool_call',
        InvocationId: 'inv-replied',
        Content: { name: 'search' },
      },
      {
        EventType: 'assistant_message',
        InvocationId: 'inv-replied',
        Content: { role: 'model', parts: [{ text: '已经回复' }] },
      },
      { EventType: 'run_status', InvocationId: 'inv-live', Content: { status: 'in_progress' } },
    ]),
    ['inv-replied', 'inv-live'],
  );
});

test('run state utils ignore stale in-progress invocations after refresh', async () => {
  const runStateUtils = await loadRunStateUtils();

  assert.ok(runStateUtils, 'expected run state helpers to exist');
  const now = Date.now();
  assert.deepEqual(
    runStateUtils.findActiveRunIds(
      [
        {
          EventType: 'run_status',
          InvocationId: 'inv-stale',
          Content: { status: 'in_progress' },
          Timestamp: now - 31 * 60 * 1000,
        },
        {
          EventType: 'run_status',
          InvocationId: 'inv-live',
          Content: { status: 'in_progress' },
          Timestamp: now - 10 * 1000,
        },
      ],
      { now, staleAfterMs: 30 * 60 * 1000 },
    ),
    ['inv-live'],
  );
});

test('run state utils build subscribe URLs with session and invocation id', async () => {
  const runStateUtils = await loadRunStateUtils();

  assert.ok(runStateUtils, 'expected run state helpers to exist');
  assert.equal(
    runStateUtils.buildSubscribeRunEventsUrl({
      sessionId: 'sess-1',
      invocationId: 'inv-1',
      afterSeqId: 7,
    }),
    '/agentengine/api/v1/SubscribeRunEvents?SessionId=sess-1&InvocationId=inv-1&AfterSeqId=7',
  );
});
