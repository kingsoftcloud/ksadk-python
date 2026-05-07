import test from 'node:test';
import assert from 'node:assert/strict';

async function loadStreamControlUtils() {
  return import('../src/utils/stream-control.js').catch(() => null);
}

test('stream control keeps reading after response.completed terminal action', async () => {
  const streamControl = await loadStreamControlUtils();

  assert.ok(streamControl, 'expected stream control helpers to exist');
  assert.equal(
    streamControl.shouldStopReadingRunStream([
      { type: 'text_final', text: 'done' },
      { type: 'terminal', status: 'completed' },
    ]),
    false,
  );
});

test('stream control stops reading failed or incomplete terminal actions', async () => {
  const streamControl = await loadStreamControlUtils();

  assert.ok(streamControl, 'expected stream control helpers to exist');
  assert.equal(
    streamControl.shouldStopReadingRunStream([{ type: 'terminal', status: 'failed' }]),
    true,
  );
  assert.equal(
    streamControl.shouldStopReadingRunStream([{ type: 'terminal', status: 'incomplete' }]),
    true,
  );
});
