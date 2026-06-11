import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const webUiRoot = resolve(__dirname, '..');
const repoRoot = resolve(webUiRoot, '../../..');

test('ksadk makefile syncs static assets from ksadk-web GitHub release tarball', () => {
  const makefile = readFileSync(resolve(repoRoot, 'Makefile'), 'utf8');
  const syncSection = makefile.split('\nsync-ksadk-web-static:', 2)[1]?.split('\n#', 1)[0] || '';

  assert.match(makefile, /KSADK_WEB_VERSION \?= v0\.2\.2/);
  assert.match(makefile, /KSADK_WEB_TARBALL_NAME := kingsoftcloud-ksadk-web-\$\(patsubst v%,%,\$\(KSADK_WEB_VERSION\)\)\.tgz/);
  assert.match(makefile, /\.PHONY: .*sync-ksadk-web-static/);
  assert.match(syncSection, /curl -fL --retry 3 --retry-delay 2 --retry-all-errors "\$\(KSADK_WEB_RELEASE_URL\)"/);
  assert.match(syncSection, /tar -xzf "\$\(KSADK_WEB_CACHE_DIR\)\/\$\(KSADK_WEB_TARBALL_NAME\)"/);
  assert.match(syncSection, /package\/dist-ksadk/);
  assert.match(syncSection, /cp -R "\$\(KSADK_WEB_CACHE_DIR\)\/package\/dist-ksadk\/\." "\$\(STATIC_DIR\)\/"/);
  assert.match(makefile, /sync-hosted-ui: sync-ksadk-web-static/);
  assert.match(makefile, /build-frontend: sync-ksadk-web-static/);
});
