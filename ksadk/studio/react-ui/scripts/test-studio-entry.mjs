// Opt-in smoke test against an already running Studio with a workspace plugin.
// Uses fresh browser contexts; does not create groups or submit model work.
import { chromium, expect } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
const origin = process.env.STUDIO_TEST_URL || 'http://127.0.0.1:8775';
const browser = await chromium.launch();
const results = [];
await mkdir('output/studio-entry', { recursive: true });
try {
  for (const width of [1440, 390]) {
    for (const entry of ['/#/agents', '/?workspacePage=teams#/workspace/teams', '/studio-core/#/workspace/teams']) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(origin + entry);
      await expect(page.locator('.app-shell')).toBeVisible();
      const expected = new URL(origin + entry);
      await expect.poll(() => new URL(page.url()).pathname).toBe('/studio-core/');
      expect(new URL(page.url()).hash).toBe(expected.hash);
      expect(new URL(page.url()).search).toBe(expected.search);
      if (width === 390) await page.getByRole('button', { name: '展开导航', exact: true }).click();
      const teams = page.getByRole('navigation', { name: '产品导航' }).getByRole('button', { name: '团队', exact: true });
      await expect(teams).toBeVisible();
      await teams.click();
      await expect(page.getByRole('complementary', { name: '团队列表' })).toBeVisible();
      await page.reload();
      await expect(page.getByRole('complementary', { name: '团队列表' })).toBeVisible();
      expect(errors).toEqual([]);
      const id = `${width}-${results.length + 1}`;
      await page.screenshot({ path: `output/studio-entry/${id}.png`, animations: 'disabled' });
      results.push({ width, entry, finalUrl: page.url(), errors });
      await context.close();
    }
  }
  await writeFile('output/studio-entry/result.json', JSON.stringify(results, null, 2));
  console.log(`${results.length} fresh-browser entry and reload checks passed`);
} finally {
  await browser.close();
}
