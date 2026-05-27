import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const webUiRoot = resolve(__dirname, '..');
const repoRoot = resolve(webUiRoot, '../../..');

test('ksadk makefile syncs embedded web-ui source from hosted UI before building static assets', () => {
  const makefile = readFileSync(resolve(repoRoot, 'Makefile'), 'utf8');
  const syncSection = makefile.split('\nsync-hosted-ui:', 2)[1]?.split('\n#', 1)[0] || '';

  assert.match(makefile, /HOSTED_UI_SOURCE_DIR \?= \.\.\/agentengine-hosted-ui/);
  assert.match(makefile, /\.PHONY: .*sync-hosted-ui/);
  assert.match(syncSection, /rsync -a --delete/);
  assert.match(syncSection, /\$\(HOSTED_UI_SOURCE_DIR\)\/src\//);
  assert.match(syncSection, /\$\(NODE_DIR\)\/src\//);
  assert.match(syncSection, /--exclude='makefile-contract\.test\.mjs'/);
  assert.match(syncSection, /--exclude='helm-contract\.test\.mjs'/);
  assert.match(syncSection, /--exclude='sync-static\.test\.mjs'/);
  assert.match(syncSection, /--exclude='hosted-ui-sync\.test\.mjs'/);
  assert.match(syncSection, /\$\(HOSTED_UI_SOURCE_DIR\)\/public\//);
  assert.match(syncSection, /npm run build:all/);
  assert.match(makefile, /build-frontend: sync-hosted-ui/);
});
