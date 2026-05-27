import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');

test('sidebar does not block session navigation while a run is streaming', () => {
  const source = readFileSync(resolve(repoRoot, 'src/components/chat/ChatSidebar.tsx'), 'utf8');

  assert.doesNotMatch(source, /disabled=\{isStreaming\}/);
  assert.doesNotMatch(source, /!isStreaming && onSelectSession/);
  assert.doesNotMatch(source, /if \(!isStreaming\) \{\s*onSelectSession/);
});

test('session switching does not disconnect the active run engine', () => {
  const source = readFileSync(resolve(repoRoot, 'src/hooks/useSessionLifecycle.ts'), 'utf8');

  assert.doesNotMatch(source, /currentSessionIdRef\.current && currentSessionIdRef\.current !== sessionId[\s\S]{0,180}disconnectRun\?\.\(\)/);
});

test('restore subscription is silent and does not lock the composer', () => {
  const lifecycleSource = readFileSync(resolve(repoRoot, 'src/hooks/useSessionLifecycle.ts'), 'utf8');
  const streamingSource = readFileSync(resolve(repoRoot, 'src/stores/streaming.ts'), 'utf8');
  const messageListSource = readFileSync(resolve(repoRoot, 'src/components/chat/ChatMessageList.tsx'), 'utf8');

  assert.doesNotMatch(lifecycleSource, /setStreaming\(true\)/);
  assert.doesNotMatch(lifecycleSource, /beginActivity\(\{[\s\S]*source: 'restore'/);
  assert.doesNotMatch(streamingSource, /phase: '已停止接收'/);
  assert.doesNotMatch(messageListSource, /\{activity\.eventCount\} events/);
  assert.doesNotMatch(messageListSource, /输出已断开/);
});

test('run dispatcher ignores streamed tokens for inactive sessions', () => {
  const dispatcherSource = readFileSync(resolve(repoRoot, 'src/core/run/dispatcher.ts'), 'utf8');
  const engineSource = readFileSync(resolve(repoRoot, 'src/core/run/engine.ts'), 'utf8');

  assert.match(dispatcherSource, /currentSessionId !== event\.sessionId/);
  assert.match(engineSource, /activeSessionId/);
});

test('composer run startup is scoped to the current session, not a global streaming lock', () => {
  const runAgentSource = readFileSync(resolve(repoRoot, 'src/hooks/useRunAgent.ts'), 'utf8');
  const composerSource = readFileSync(resolve(repoRoot, 'src/components/chat/ConnectedComposer.tsx'), 'utf8');
  const messageListSource = readFileSync(resolve(repoRoot, 'src/components/chat/ConnectedMessageList.tsx'), 'utf8');

  assert.doesNotMatch(runAgentSource, /engine\.stage !== 'idle' \|\| useStreamingStore\.getState\(\)\.isStreaming/);
  assert.match(runAgentSource, /isSessionStreaming\(/);
  assert.match(composerSource, /getSessionActivity\(currentSessionId\)/);
  assert.match(messageListSource, /getSessionActivity\(currentSessionId\)/);
});

test('run capsule shows low-noise animated token counts without sidebar clutter', () => {
  const messageListSource = readFileSync(resolve(repoRoot, 'src/components/chat/ChatMessageList.tsx'), 'utf8');
  const connectedMessageListSource = readFileSync(resolve(repoRoot, 'src/components/chat/ConnectedMessageList.tsx'), 'utf8');
  const sidebarSource = readFileSync(resolve(repoRoot, 'src/components/chat/ChatSidebar.tsx'), 'utf8');

  assert.match(connectedMessageListSource, /buildComposerContextIndicator/);
  assert.match(messageListSource, /AnimatedTokenCount/);
  assert.match(messageListSource, /token-count-pulse/);
  assert.match(messageListSource, /估算 token/);
  assert.doesNotMatch(sidebarSource, /usedTokens|contextWindowTokens|token-count-pulse/);
});
