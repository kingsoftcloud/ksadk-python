import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const webUiRoot = resolve(__dirname, '..');

test('sync-static clears stale static files before copying the current build', () => {
  const script = readFileSync(resolve(webUiRoot, 'scripts/sync-static.mjs'), 'utf8');

  assert.match(script, /await rm\(staticDir, \{ recursive: true, force: true \}\);/);
  assert.match(script, /await mkdir\(staticDir, \{ recursive: true \}\);/);
  assert.doesNotMatch(script, /await rm\(staticAssetsDir/);
});
