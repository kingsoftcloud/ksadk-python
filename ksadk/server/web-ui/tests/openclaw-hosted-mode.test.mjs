import test from 'node:test';
import assert from 'node:assert/strict';

async function loadOpenClawHostedModeUtils() {
  return import('../src/utils/openclaw-hosted-mode.js').catch(() => null);
}

test('openclaw hosted mode uses native launcher instead of generic chat', async () => {
  const openclawHostedMode = await loadOpenClawHostedModeUtils();

  assert.ok(openclawHostedMode, 'expected openclaw hosted mode helpers to exist');
  assert.equal(openclawHostedMode.shouldUseOpenClawNativeLauncher('openclaw'), true);
  assert.equal(openclawHostedMode.shouldUseOpenClawNativeLauncher('OpenClaw'), true);
  assert.equal(openclawHostedMode.shouldUseOpenClawNativeLauncher('hermes'), false);
  assert.equal(openclawHostedMode.shouldUseOpenClawNativeLauncher('langgraph'), false);
  assert.equal(openclawHostedMode.shouldUseOpenClawNativeLauncher(''), false);
});
