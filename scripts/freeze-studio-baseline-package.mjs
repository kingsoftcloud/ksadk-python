/** Freeze the actual accepted package, including release-owned non-runtime files. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
const tools = resolve('qualification-tools');
const require = createRequire(join(tools, 'package.json'));
const asar = require('@electron/asar');
const { installedStudioFingerprint } = await import(join(tools, 'scripts/installed-studio-fingerprint.mjs'));
const archive = resolve(process.env.STUDIO_BASELINE_ASAR);
const output = resolve(process.env.STUDIO_BASELINE_PACKAGE);
const identity = installedStudioFingerprint(archive);
assert.equal(identity.studioSourceSha256, 'b9d8a44e2baea87a1b449aba373eae865a6322201d59ac1741f80e532e7221a6');
assert.equal(identity.studioPackageSha256, 'a6a80b62e519e57c4095dedb2a83c078ab2fdb4e65745ca7b7035fa55d17986e');
const prefix = '/dsh/node_modules/@kingsoftcloud/agentkit-studio/';
let files = 0;
for (const path of asar.listPackage(archive).filter(path => path.startsWith(prefix)).sort()) {
  const info = asar.statFile(archive, path.slice(1), false);
  if (info.files) continue;
  assert.ok(!info.link && !info.unpacked);
  const name = path.slice(prefix.length);
  assert.ok(name && !name.startsWith('/') && !name.split('/').includes('..'));
  const target = join(output, 'package', name);
  await mkdir(join(target, '..'), { recursive: true });
  await writeFile(target, asar.extractFile(archive, path.slice(1)), { flag: 'wx', mode: info.executable ? 0o755 : 0o644 });
  files += 1;
}
await writeFile('rollback-frozen-package.json', JSON.stringify({ ...identity, files,
  source: 'exact signed desktop-0.2.0-rc.2.20261014.10 package',
  nativeHostSource: '9b20aee585956c30239ab1775098cdab25ada9cb',
  additionalHostPackagingPatch: '039', published: false }, null, 2) + '\n');
console.log(JSON.stringify({ originalAcceptedPackageFrozen: true, files, ...identity }));
