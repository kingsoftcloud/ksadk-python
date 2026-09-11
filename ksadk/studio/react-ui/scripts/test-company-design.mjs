// Opt-in, read-only visual audit of an already running Studio with Teams enabled.
// Run from react-ui: STUDIO_TEST_URL=http://127.0.0.1:8775 node scripts/test-company-design.mjs
// Uses real data; never submits forms, sends model messages, or saves settings.
import { chromium, expect } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';

const origin = (process.env.STUDIO_TEST_URL || 'http://127.0.0.1:8775').replace(/\/$/, '');
const output = 'output/king-design';
const widths = (process.env.STUDIO_TEST_WIDTHS || '1440,768,390').split(',').map(Number);
const themes = (process.env.STUDIO_TEST_THEMES || 'light,dark').split(',');
// Focused reruns retain the full matrix's evidence file.
const resultPath = `${output}/company-design-result${process.env.STUDIO_TEST_WIDTHS ? `-${widths.join('-')}` : ''}${process.env.STUDIO_TEST_THEMES ? `-${themes.join('-')}` : ''}.json`;
const browser = await chromium.launch();
const results = [];
const failures = [];
const readOnlyChecks = [];
// Read actions are POST RPCs in Studio's shared chat API (studio/api.py).
const readActions = new Set(['GetAgentUiBootstrap', 'ListAgentModels', 'ListSessions',
  'GetSession', 'ListSessionMessages', 'ListSessionEvents', 'GetAgentStatus',
  'GetResponseFeedback', 'ListWorkspaceFiles']);
await mkdir(output, { recursive: true });

async function settle(page, theme) {
  await page.evaluate(theme => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }, theme);
  await page.evaluate(() => document.fonts.ready);
  // Event streams remain open. Wait for visual geometry to settle instead of networkidle.
  await expect.poll(async () => page.evaluate(async () => {
    const size = () => [...document.querySelectorAll('.app-shell, main, dialog[open], [role="dialog"]')]
      .map(element => { const r = element.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; });
    const before = JSON.stringify(size());
    await new Promise(resolve => setTimeout(resolve, 180));
    return before === JSON.stringify(size());
  }), { timeout: 10000 }).toBe(true);
}

async function audit(page, width, theme, state, errors, extra = {}) {
  await settle(page, theme);
  const metrics = await page.evaluate(() => {
    const visible = element => {
      const r = element.getBoundingClientRect();
      const s = getComputedStyle(element);
      return r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < innerHeight
        && r.right > 0 && r.left < innerWidth && s.visibility !== 'hidden'
        && s.display !== 'none' && s.opacity !== '0'
        && !element.closest('[aria-hidden="true"], .sr-only, .team-sr-only');
    };
    const identify = element => ({ tag: element.tagName, id: element.id,
      className: element.className, label: element.getAttribute('aria-label') || '' });
    const style = element => {
      const r = element.getBoundingClientRect(), s = getComputedStyle(element);
      return { ...identify(element), height: +r.height.toFixed(2), radius: s.borderRadius,
        color: s.color, background: s.backgroundColor, fontSize: s.fontSize,
        borderBottom: s.borderBottom, borderBottomWidth: s.borderBottomWidth };
    };
    const dialogs = [...document.querySelectorAll('dialog[open], [role="dialog"]')].filter(visible);
    const scope = dialogs.at(-1) || document.querySelector('main') || document.body;
    const fields = [...scope.querySelectorAll('input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]), select, textarea, button[role="combobox"]')].filter(visible);
    const textRects = [];
    for (const label of scope.querySelectorAll('label, legend, .form-field-label')) {
      if (!visible(label)) continue;
      const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (!node.textContent.trim() || node.parentElement.closest('input, select, textarea, button, [aria-hidden="true"]')) continue;
        const range = document.createRange(); range.selectNodeContents(node);
        for (const r of range.getClientRects()) {
          if (r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < innerHeight)
            textRects.push({ label: node.textContent.trim().slice(0, 80), rect: r });
        }
      }
    }
    const overlaps = [];
    for (const field of fields) {
      const r = field.getBoundingClientRect();
      for (const text of textRects) {
        if (Math.min(r.right, text.rect.right) - Math.max(r.left, text.rect.left) > 2
          && Math.min(r.bottom, text.rect.bottom) - Math.max(r.top, text.rect.top) > 2)
          overlaps.push({ field: identify(field), label: text.label });
      }
    }
    const controls = [...scope.querySelectorAll('.button, .primary-button, .studio-select-trigger, .team-button, .team-field > input, .team-field > select')].filter(visible).map(style);
    const selected = [...document.querySelectorAll('.studio-nav-link[aria-current="page"], .page-tabs [aria-selected="true"], .studio-team-list-item[aria-current="page"]')].filter(visible).map(style);
    const tabs = [...scope.querySelectorAll('.page-tabs [role="tab"]')].filter(visible).map(style);
    const fonts = [...document.fonts].filter(font => font.family.includes('Studio King Design Icons'))
      .map(font => ({ family: font.family, status: font.status }));
    const main = document.querySelector('main');
    return { rootOverflow: Math.max(0, document.documentElement.scrollWidth - innerWidth),
      mainOverflow: main ? Math.max(0, main.scrollWidth - main.clientWidth) : 0,
      labelOverlaps: overlaps, controls, selected, tabs, fields: fields.map(style), fonts,
      officialIconCount: document.querySelectorAll('.king-icon').length,
      localIconLoaded: document.fonts.check('16px "Studio King Design Icons"', '\ue999'),
      theme: document.documentElement.dataset.theme };
  });
  const violations = [];
  if (metrics.rootOverflow > 1) violations.push(`document overflow ${metrics.rootOverflow}px`);
  if (metrics.mainOverflow > 1) violations.push(`main overflow ${metrics.mainOverflow}px`);
  if (metrics.labelOverlaps.length) violations.push(`${metrics.labelOverlaps.length} label/input overlaps`);
  if (!metrics.fonts.length || metrics.fonts.some(font => font.status !== 'loaded') || !metrics.localIconLoaded || !metrics.officialIconCount)
    violations.push('official King Design icon font is missing or not loaded');
  if (errors.length) violations.push(`${errors.length} page errors`);
  // Capture complete measurements. Assert the canonical controls only: icon buttons,
  // compact toolbar actions, multiline textareas and wrapping actions have other sizes.
  const canonical = metrics.controls.filter(control => !/small|mini|icon|expand|history|compact/.test(control.className));
  for (const control of canonical) {
    if (![32, 40].some(height => Math.abs(control.height - height) <= 1))
      violations.push(`control height ${control.height}px: ${control.className}`);
    if (control.radius !== '4px') violations.push(`control radius ${control.radius}: ${control.className}`);
  }
  for (const tab of metrics.tabs) {
    if (tab.borderBottomWidth !== '2px') violations.push(`tab underline ${tab.borderBottomWidth}`);
  }
  const id = `${width}-${theme}-${state}`;
  await page.screenshot({ path: `${output}/${id}.png`, animations: 'disabled',
    mask: [page.locator('input[type="password"]')] });
  const result = { id, width, theme, state, url: page.url(), metrics, pageErrors: [...errors], violations, ...extra };
  results.push(result);
  if (violations.length) failures.push({ id, violations });
  await writeFile(resultPath, JSON.stringify({ results, failures, readOnlyChecks }, null, 2));
  console.log(`${id}: ${violations.length ? violations.join('; ') : 'pass'}`);
}

async function navigate(page, hash, selector, theme) {
  await page.evaluate(hash => { location.hash = hash; }, hash);
  await expect(page.locator(selector)).toBeVisible({ timeout: 20000 });
  await settle(page, theme);
}

async function showNav(page) {
  const nav = page.getByRole('navigation', { name: '产品导航' });
  if (!await nav.isVisible()) await page.getByRole('button', { name: '展开导航', exact: true }).click();
  await expect(nav).toBeVisible();
  return nav;
}

try {
  for (const width of widths) for (const theme of themes) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: theme });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [], blockedWrites = [], expectedBlocked = [], allowedReadActions = new Set();
    page.on('pageerror', error => errors.push(error.message));
    // Fail closed on application writes. Core's own transport is outside /api/v1.
    await context.route(/\/(?:api\/v\d\/|v1\/responses)/, async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (request.method() === 'POST' && /^\/api\/v1\/groups\/[^/]+\/read$/.test(path)) {
        // Opening a team automatically sends a read receipt. Suppress this write
        // and return its empty success response; all actual content remains real.
        expectedBlocked.push({ method: request.method(), path });
        return route.fulfill({ status: 204 });
      }
      if (request.method() === 'POST' && path.startsWith('/agentengine/api/v1/') && readActions.has(path.split('/').at(-1))) {
        allowedReadActions.add(path.split('/').at(-1));
        return route.continue();
      }
      if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
        blockedWrites.push({ method: request.method(), path });
        await route.abort('blockedbyclient');
      } else await route.continue();
    });
    try {
      await page.goto(`${origin}/#/agents`);
      await expect(page.locator('.agents-page')).toBeVisible({ timeout: 30000 });
      await expect(page.locator('.agents-page [role="status"]')).toHaveCount(0);
      await audit(page, width, theme, 'agents', errors);

      const select = page.locator('.agents-page .studio-select-trigger').first();
      if (await select.isVisible()) {
        await select.click();
        await expect(page.getByRole('listbox')).toBeVisible();
        await page.keyboard.press('ArrowDown');
        await audit(page, width, theme, 'agents-filter-open', errors);
        await page.keyboard.press('Escape');
        await expect(select).toBeFocused();
      }

      await navigate(page, '#/create', '.create-shell', theme);
      await audit(page, width, theme, 'create-agent', errors);

      const nav = await showNav(page);
      const resourceGroup = nav.getByRole('button', { name: '资源库', exact: true });
      if (await resourceGroup.getAttribute('aria-expanded') !== 'true') await resourceGroup.click();
      await expect(nav.getByRole('button', { name: '模型与工具', exact: true })).toBeVisible();
      await audit(page, width, theme, 'resource-navigation', errors);
      await nav.getByRole('button', { name: '模型与工具', exact: true }).click();
      await expect(page.getByRole('tablist', { name: '资源类型' })).toBeVisible();
      await audit(page, width, theme, 'resources-models', errors);
      await page.getByRole('tab', { name: /^Tool/ }).click();
      await audit(page, width, theme, 'resources-tools', errors);

      await navigate(page, '#/plugins', '.plugins-page', theme);
      await audit(page, width, theme, 'plugins', errors);
      await navigate(page, '#/workspace/teams', '.studio-team-directory', theme);
      await expect(page.getByText('正在读取团队…', { exact: true })).toHaveCount(0);
      await audit(page, width, theme, 'teams-directory', errors);
      await page.getByRole('complementary', { name: '团队列表' }).getByRole('button', { name: '创建团队', exact: true }).click();
      const createTeam = page.getByRole('dialog', { name: '创建团队', exact: true });
      await expect(createTeam).toBeVisible();
      await expect(createTeam.getByText('正在读取可用 Agent…', { exact: true })).toHaveCount(0);
      await audit(page, width, theme, 'create-team-dialog', errors);
      await createTeam.getByRole('button', { name: '关闭创建团队', exact: true }).click();

      const groups = page.locator('.studio-team-list-item');
      if (await groups.count()) {
        const completed = groups.filter({ hasText: 'Agent Teams 本地验收' });
        await (await completed.count() ? completed.first() : groups.first()).click();
        await expect(page.locator('.team-workspace')).toBeVisible({ timeout: 20000 });
        await expect(page.getByRole('textbox', { name: '给团队的消息', exact: true })).toBeVisible();
        await audit(page, width, theme, 'team-existing', errors);
      } else {
        failures.push({ id: `${width}-${theme}-team-existing`, violations: ['No existing team available; existing-team view not audited'] });
      }

      await showNav(page);
      await page.getByRole('button', { name: '设置', exact: true }).filter({ has: page.locator('.king-icon') }).click();
      const settings = page.getByRole('dialog', { name: '设置', exact: true });
      await expect(settings).toBeVisible();
      await audit(page, width, theme, 'settings-general', errors);
      await settings.getByRole('button', { name: '云端连接', exact: true }).click();
      await audit(page, width, theme, 'settings-cloud', errors);
      await settings.getByRole('button', { name: '取消', exact: true }).click();
      // Hash entry displays the conversation workspace without clicking 新对话,
      // which explicitly creates a session in the product.
      await navigate(page, '#/conversations', '.app-shell[data-view="conversations"]', theme);
      await expect(page.locator('.chat-target-loading')).toHaveCount(0);
      await expect(page.locator('.studio-composer-area')).toBeVisible({ timeout: 20000 });
      await audit(page, width, theme, 'conversation', errors);
      if (blockedWrites.length) failures.push({ id: `${width}-${theme}-read-only`, violations: blockedWrites });
    } catch (error) {
      failures.push({ id: `${width}-${theme}-navigation`, violations: [error.message], blockedWrites });
      await page.screenshot({ path: `${output}/${width}-${theme}-failed.png`, animations: 'disabled', mask: [page.locator('input[type="password"]')] }).catch(() => {});
      console.error(`${width}-${theme}: ${error.message}`);
    } finally {
      readOnlyChecks.push({ width, theme, blockedWrites, expectedBlocked, allowedReadActions: [...allowedReadActions] });
      await context.close();
      await writeFile(resultPath, JSON.stringify({ results, failures, readOnlyChecks }, null, 2));
    }
  }
} finally {
  await browser.close();
}
console.log(`${results.length} real-page states audited; ${failures.length} failures. See ${resultPath}`);
if (failures.length) process.exitCode = 1;
