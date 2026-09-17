import { defineConfig } from '@playwright/test';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

const root = import.meta.dirname;
const library = path.join(root, 'node_modules/@kingsoftcloud/ksadk-web/dist-lib');
const fingerprint = files => {
  const hash = createHash('sha256');
  for (const file of [...files].sort()) hash.update(file).update(readFileSync(path.resolve(root, file)));
  return hash.digest('hex');
};

export default defineConfig({
  testDir: './e2e', testMatch: 'history-search.spec.ts', workers: 1, timeout: 45_000,
  outputDir: 'output/playwright/history-search-results',
  reporter: [['list'], ['json', { outputFile: 'output/playwright/history-search-report.json' }]],
  metadata: {
    fixture: 'synthetic-2000-messages; production renderer build; no runtime execution',
    studioSourceSha256: fingerprint(['e2e/history-search-fixture.tsx', 'src/components/ConversationFind.tsx',
      'src/components/CompactHarnessTimeline.tsx', 'src/compactHarnessMessages.ts', 'src/studio.css',
      'src/index.css', 'src/kingdesign.css', 'src/studio-refinement.css', 'src/layout-simplification.css']),
    webLibrarySha256: fingerprint(readdirSync(library).filter(file => file.endsWith('.js'))
      .map(file => path.join('node_modules/@kingsoftcloud/ksadk-web/dist-lib', file))),
  },
  use: { baseURL: 'http://127.0.0.1:4193', viewport: { width: 1440, height: 1000 },
    channel: process.env.STUDIO_BROWSER_CHANNEL || undefined,
    screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: {
    command: 'npx vite build --config vite.history-search.config.ts && npx vite preview --outDir output/playwright/history-search-dist --host 127.0.0.1 --port 4193 --strictPort',
    url: 'http://127.0.0.1:4193/e2e/history-search.html', reuseExistingServer: false,
  },
});
