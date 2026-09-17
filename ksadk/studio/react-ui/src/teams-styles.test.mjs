import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'vite';

test('the Studio entry ships the Teams component stylesheet, not only host token overrides', async () => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  // Exercise the real entry and bundler: component DOM tests cannot detect a missing CSS import.
  const result = await build({
    root,
    configFile: path.join(root, 'vite.config.ts'),
    logLevel: 'silent',
    build: { write: false },
  });
  const outputs = (Array.isArray(result) ? result : [result]).flatMap(bundle => bundle.output);
  const html = String(outputs.find(output => output.fileName === 'index.html')?.source || '');
  const styles = outputs.filter(output => output.type === 'asset'
    && output.fileName.endsWith('.css') && html.includes(output.fileName));
  assert.ok(styles.length, 'The Studio document must link to its bundled stylesheet');
  const css = styles.map(output => String(output.source)).join('\n');
  for (const selector of ['.ksadk-teams.team-create-dialog', '.ksadk-teams.team-workspace', '.ksadk-teams .team-candidate']) {
    assert.ok(css.includes(`${selector}{`) || css.includes(`${selector} {`),
      `Missing shared Teams layout rules: ${selector}`);
  }
});
