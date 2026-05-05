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
