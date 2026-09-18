import { expect, test } from '@playwright/test';

for (const mode of ['shared', 'compact']) {
  test(`${mode}: find and reveal unmounted history without replacing the draft`, async ({ page }, info) => {
    await page.goto(`/e2e/history-search.html?mode=${mode}`);
    const draft = page.getByRole('textbox', { name: '下一条消息' });
    await draft.fill('正在编辑的下一条消息');
    await expect(page.locator('[data-message-id="message-802"]')).toHaveCount(0);
    await page.getByRole('searchbox', { name: '查找当前会话正文' }).fill('验收目标');
    await expect(page.getByRole('status').filter({ hasText: '已查找全部历史，3 条消息匹配' })).toBeVisible();
    const panel = await page.getByRole('region', { name: '查找当前会话', exact: true }).boundingBox();
    const timeline = await page.locator('.studio-chat-timeline').boundingBox();
    expect(panel!.y + panel!.height).toBeLessThanOrEqual(timeline!.y + 1);
    await page.getByRole('button', { name: /验收目标位于更早的历史.*802/ }).click();
    const target = page.locator('[data-search-target="true"]');
    await expect(target).toHaveCount(1);
    await expect(target).toContainText('验收目标位于更早的历史');
    await expect(target).toBeFocused();
    const rect = await target.boundingBox();
    expect(rect).not.toBeNull();
    expect(rect!.y).toBeGreaterThan(0);
    expect(rect!.y).toBeLessThan(800);
    await expect(draft).toHaveValue('正在编辑的下一条消息');
    if (mode === 'shared') expect(await page.locator('[data-message-id]').count()).toBeLessThan(100);
    await page.screenshot({ path: info.outputPath('history-search.png') });
    await info.attach('history-search', { path: info.outputPath('history-search.png'), contentType: 'image/png' });
  });
}
