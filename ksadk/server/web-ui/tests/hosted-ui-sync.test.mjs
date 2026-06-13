import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const webUiRoot = resolve(__dirname, '..');
const repoRoot = resolve(webUiRoot, '../../..');

test('ksadk makefile syncs static assets from ksadk-web npm package by default', () => {
  const makefile = readFileSync(resolve(repoRoot, 'Makefile'), 'utf8');
  const syncSection = makefile.split('\nsync-ksadk-web-static:', 2)[1]?.split('\n#', 1)[0] || '';

  assert.match(makefile, /KSADK_WEB_VERSION \?= latest/);
  assert.match(makefile, /KSADK_WEB_PACKAGE \?= @kingsoftcloud\/ksadk-web/);
  assert.match(makefile, /KSADK_WEB_TARBALL_NAME := kingsoftcloud-ksadk-web-\$\(patsubst v%,%,\$\(KSADK_WEB_VERSION\)\)\.tgz/);
  assert.match(makefile, /KSADK_WEB_RELEASE_URL \?=/);
  assert.match(makefile, /\.PHONY: .*sync-ksadk-web-static/);
  assert.match(syncSection, /npm pack "\$\(KSADK_WEB_PACKAGE\)@\$\(patsubst v%,%,\$\(KSADK_WEB_VERSION\)\)"/);
  assert.match(syncSection, /\.tarball-name/);
  assert.match(syncSection, /if \[ -n "\$\(KSADK_WEB_RELEASE_URL\)" \]/);
  assert.match(syncSection, /curl -fL --retry 3 --retry-delay 2 --retry-all-errors "\$\(KSADK_WEB_RELEASE_URL\)"/);
  assert.match(syncSection, /tar -xzf "\$\(KSADK_WEB_CACHE_DIR\)\/\$\$\(cat "\$\(KSADK_WEB_CACHE_DIR\)\/\.tarball-name"\)"/);
  assert.match(syncSection, /package\/dist-ksadk/);
  assert.match(syncSection, /cp -R "\$\(KSADK_WEB_CACHE_DIR\)\/package\/dist-ksadk\/\." "\$\(STATIC_DIR\)\/"/);
  assert.match(makefile, /sync-hosted-ui: sync-ksadk-web-static/);
  assert.match(makefile, /build-frontend: sync-ksadk-web-static/);
});

test('bundled dashboard static includes the full checkpoint resume panel', () => {
  const staticRoot = resolve(repoRoot, 'ksadk/server/static');
  const indexHtml = readFileSync(resolve(staticRoot, 'index.html'), 'utf8');
  const scriptMatches = [...indexHtml.matchAll(/<script[^>]+src="\.\/assets\/([^"]+\.js)"/g)];
  assert.ok(scriptMatches.length > 0, 'expected dashboard static index.html to reference JS assets');

  const bundledScripts = scriptMatches
    .map((match) => readFileSync(resolve(staticRoot, 'assets', match[1]), 'utf8'))
    .join('\n');

  assert.match(bundledScripts, /会话恢复区/);
  assert.match(bundledScripts, /选择 LangGraph 状态快照，从对应图状态继续/);
  assert.match(bundledScripts, /第 \$\{Math\.max\(1,[a-z]-[a-z]\)\}\/\$\{Math\.max\(1,[a-z]\)\} 阶段/);
  assert.match(bundledScripts, /最新/);
  assert.doesNotMatch(bundledScripts, /个可恢复点/);
});
