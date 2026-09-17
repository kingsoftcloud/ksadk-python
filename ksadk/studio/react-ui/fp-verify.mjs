import { chromium } from 'playwright';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
await page.goto('http://127.0.0.1:8080/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3000);
await page.evaluate(() => {
  window.dispatchEvent(new CustomEvent('ksadk-workspace-file-preview', { detail: { path: '/Users/xiayu/agentengine-test/studio-test/AI芯片最新消息_2026年9月.md' } }));
});
await page.waitForTimeout(1500);
const dialog = page.locator('[role="dialog"]');
const styles = await dialog.evaluate(el => {
  const s = getComputedStyle(el);
  const inner = el.querySelector('.max-h-full');
  const si = inner ? getComputedStyle(inner) : null;
  return {
    overlayBg: s.backgroundColor,
    overlayZ: s.zIndex,
    panelBorder: si?.borderTopWidth,
    panelBg: si?.backgroundColor,
    panelRadius: si?.borderRadius,
    headerBorder: getComputedStyle(el.querySelector('header')).borderBottomWidth,
    titleSize: getComputedStyle(el.querySelector('h2')).fontSize,
  };
});
console.log(JSON.stringify(styles, null, 1));
console.log('title:', await dialog.locator('h2').first().textContent());
console.log('errors:', errors.length ? errors : '无');
await page.screenshot({ path: '/tmp/fp-styled.png' });
await browser.close();
