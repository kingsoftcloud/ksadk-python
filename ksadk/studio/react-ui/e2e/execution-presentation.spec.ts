import { expect, test } from "@playwright/test";

test("execution cards, thinking disclosure and virtual history keep reading stable", async ({ page }, info) => {
  await page.goto("/e2e/execution-presentation.html");
  await expect(page.getByText("任务计划", { exact: true })).toBeVisible();
  await expect(page.getByText("1/2", { exact: true })).toBeVisible();
  await page.locator(".harness-live-thinking-head").click();
  await page.getByRole("button", { name: "结束流式" }).click();
  await expect(page.locator(".harness-live-thinking-details")).toHaveAttribute("open", "");
  await page.getByText("执行记录", { exact: true }).click();
  await page.getByText("运行检查", { exact: true }).click();
  await page.getByText("运行命令：pytest", { exact: true }).click();
  const output = page.getByLabel("工具输出");
  await expect(output).toBeVisible();
  await expect(page.getByText("退出码 1", { exact: true })).toBeVisible();
  await expect(page.getByText(/输出已截断/)).toBeVisible();
  await page.getByText("编辑文件：app.py", { exact: true }).click();
  await expect(page.locator('[data-change="add"]')).toContainText("+decision = observed_decision");
  await page.screenshot({ path: info.outputPath("execution-cards.png") });
  await info.attach("execution-cards", { path: info.outputPath("execution-cards.png"), contentType: "image/png" });
  // Fold the facts to recover timeline space, then inspect the old history.
  await page.getByText("执行记录", { exact: true }).click();
  const timeline = page.locator(".harness-timeline");
  await timeline.evaluate(node => { node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - 1000); });
  await expect(page.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  const anchor = await timeline.evaluate(node => {
    const top = node.getBoundingClientRect().top;
    const row = [...node.querySelectorAll<HTMLElement>('.harness-turn:not([data-windowed="true"])')]
      .find(candidate => candidate.getBoundingClientRect().top >= top);
    return { id: row!.dataset.messageId!, top: row!.getBoundingClientRect().top };
  });
  await page.getByRole("button", { name: "追加输出" }).click();
  await expect.poll(async () => Math.abs((await page.locator(`.harness-turn[data-message-id="${anchor.id}"]`).boundingBox())!.y - anchor.top)).toBeLessThan(3);
  await page.getByRole("button", { name: "回到最新消息" }).click();
  await expect.poll(() => timeline.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop)).toBeLessThan(5);
  await expect.poll(() => page.locator('.harness-turn:not([data-windowed="true"])').count()).toBeLessThan(100);
});
