import { chromium } from 'playwright';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
const results = {};

async function open(path) {
  await page.evaluate((p) => {
    window.dispatchEvent(new CustomEvent('ksadk-workspace-file-preview', { detail: { path: p } }));
  }, path);
  await page.waitForTimeout(900);
}
async function close() {
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
}
async function state() {
  return page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) return { open: false };
    const ps = getComputedStyle(dialog.querySelector('header') ? dialog.firstElementChild : dialog);
    return {
      open: true,
      bg: ps.backgroundColor,
      border: ps.borderTopWidth,
      radius: ps.borderRadius,
      title: dialog.querySelector('h2')?.textContent,
      text: (dialog.textContent || '').slice(0, 200),
      hasImg: !!dialog.querySelector('img'),
      alert: dialog.querySelector('[role="alert"]')?.textContent || null,
    };
  });
}

await page.goto('http://127.0.0.1:8080/', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(4000);

// 1 markdown
await open('/Users/xiayu/agentengine-test/studio-test/AI芯片最新消息_2026年9月.md');
results.markdown = { ...(await state()), contentOk: (await state()).text.includes('AI 芯片最新消息汇总') };
await close();

// 2 utf-8 文本
await open('/Users/xiayu/agentengine-test/studio-test/预览测试_文本.txt');
results.textUtf8 = await state();
await close();

// 3 GBK 日志
await open('/Users/xiayu/agentengine-test/studio-test/预览测试_gbk.log');
results.gbk = await state();
await close();

// 4 图片
await open('/Users/xiayu/agentengine-test/studio-test/预览测试_图片.png');
results.image = await state();
await close();

// 5 越界路径
await open('/etc/passwd');
results.forbidden = await state();
await close();

// 6 markdown 重新打开确认状态干净
await open('/Users/xiayu/agentengine-test/studio-test/AI芯片最新消息_2026年9月.md');
results.reopen = { contentOk: (await state()).text.includes('AI 芯片最新消息汇总'), alertCleared: (await state()).alert === null };
await close();

results.pageErrors = errors.length ? errors.slice(0, 3) : '无';
console.log(JSON.stringify(results, null, 1));
await page.screenshot({ path: '/tmp/fp-full.png' });
await browser.close();
