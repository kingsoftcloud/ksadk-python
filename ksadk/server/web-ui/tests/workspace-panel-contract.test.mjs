import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');

test('workspace panel auto-refreshes the current directory without replacing dirty edits', () => {
  const source = readFileSync(resolve(repoRoot, 'src/components/workspace/WorkspacePanel.tsx'), 'utf8');

  assert.match(source, /WORKSPACE_AUTO_REFRESH_MS/);
  assert.match(source, /window\.setInterval/);
  assert.match(source, /if \(!open \|\| dirty \|\| !initialized\)/);
  assert.match(source, /loadEntries\(currentPath, \{ background: true \}\)/);
});

test('workspace background refresh preserves the active preview/editor state', () => {
  const source = readFileSync(resolve(repoRoot, 'src/components/workspace/WorkspacePanel.tsx'), 'utf8');

  assert.match(source, /lastLoadedPreviewPathRef/);
  assert.match(source, /previewLoadReasonRef/);
  assert.match(source, /previewLoadReasonRef\.current === 'background-refresh'/);
  assert.match(source, /lastLoadedPreviewPathRef\.current === selectedEntry\.Path/);
});
