/** Verify the real NSIS-installed app and retained conversation on a disposable Runner. */
import assert from 'node:assert/strict';
import { _electron, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import asar from '@electron/asar';

assert.equal(process.platform, 'win32');
assert.equal(process.env.GITHUB_ACTIONS, 'true');
assert.equal(process.env.RUNNER_OS, 'Windows');
const { values } = parseArgs({ options: Object.fromEntries(['executable', 'version', 'source', 'report'].map(name => [name, { type: 'string' }])) });
for (const key of ['executable', 'version', 'source', 'report']) assert.ok(values[key], `Specify --${key}`);
assert.match(values.source, /^[0-9a-f]{40}$/);
const output = resolve(values.report), executablePath = resolve(values.executable);
await mkdir(dirname(output), { recursive: true });
const userMessage = 'Windows refactor acceptance marker: retain this conversation after restarting.';
const assistantMessage = 'Windows refactor acceptance reply retained.';
const server = createServer((request, response) => {
  request.resume();
  if (request.url === '/v1/models') {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ object: 'list', data: [{ id: 'native-proof', object: 'model' }] }));
    return;
  }
  response.setHeader('Content-Type', 'text/event-stream');
  for (const [content, finish] of [[assistantMessage, null], ['', 'stop']]) {
    response.write('data: ' + JSON.stringify({ id: 'native-proof', object: 'chat.completion.chunk', model: 'native-proof', created: 1,
      choices: [{ index: 0, delta: finish ? {} : { role: 'assistant', content }, finish_reason: finish }] }) + '\n\n');
  }
  response.end('data: [DONE]\n\n');
});
await new Promise((accept, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', accept); });
const environment = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/(KEY|SECRET|TOKEN|PASSWORD|CERTUM|PSMODULEPATH)/i.test(name)));
delete environment.DSH_HOME;
delete environment.AGENTKIT_HOME;
Object.assign(environment, { DSH_TELEMETRY_DISABLED: '1', DSH_DESKTOP_OPEN_DEVTOOLS: '0' });
const report = { evidence: 'real-nsis-installed-windows-app-with-controlled-provider', sourceCommit: values.source,
  version: values.version, launches: [], passed: false,
  limits: ['Unsigned diagnostic installer; no signature or updater acceptance.', 'Loopback provider proves native Host integration and retention, not a real external model.'] };
let app, page, sessionId;
try {
  const archive = join(dirname(executablePath), 'resources/app.asar');
  report.packagedFiles = {};
  for (const file of ['src/index.mjs', 'dist/client.js']) {
    const bytes = asar.extractFile(archive, `dsh/node_modules/@kingsoftcloud/agentkit-studio/${file}`);
    assert.ok(bytes.equals(await readFile(file)), `Installed ${file} must match the verified source`);
    report.packagedFiles[file] = createHash('sha256').update(bytes).digest('hex');
  }
  for (let restart = 0; restart < 2; restart++) {
    const errors = [], started = Date.now();
    app = await _electron.launch({ executablePath, cwd: dirname(executablePath), env: environment, timeout: 120000 });
    page = await app.firstWindow({ timeout: 120000 });
    page.on('pageerror', error => errors.push(error.name));
    await expect(page.getByRole('button', { name: 'Agents', exact: true })).toBeVisible({ timeout: 120000 });
    const runtime = await app.evaluate(({ app }) => ({ version: app.getVersion(), isPackaged: app.isPackaged, node: process.versions.node, electron: process.versions.electron }));
    assert.equal(runtime.isPackaged, true);
    assert.equal(runtime.version, values.version);
    if (restart === 0) {
      const guide = page.locator('.agentkit-credential-onboarding-content');
      await expect(guide).toBeVisible({ timeout: 120000 });
      await guide.getByRole('button', { name: /^(开始设置|去配置模型凭证)$/ }).click();
      const models = page.locator('[data-studio-model-settings]');
      await expect(models.getByRole('button', { name: '保存配置', exact: true })).toBeEnabled({ timeout: 90000 });
      await models.getByRole('combobox', { name: /^提供商/ }).first().selectOption('custom');
      await models.getByLabel('模型名称', { exact: true }).first().fill('native-proof');
      await models.getByLabel('API 地址', { exact: true }).first().fill(`http://127.0.0.1:${server.address().port}/v1`);
      await models.getByLabel(/^API Key/).first().fill('fixture-native-only');
      await models.getByRole('button', { name: '保存配置', exact: true }).click();
      await expect(models).toContainText('已保存多个模型配置', { timeout: 90000 });
      await page.keyboard.press('Escape');
      await expect(models).toBeHidden({ timeout: 30000 });
      await page.getByRole('treeitem').filter({ hasText: /Default workspace|默认工作区/ }).first().hover();
      await page.getByRole('button', { name: /New session in|中新建会话/ }).first().click();
      const composer = page.locator('[contenteditable="true"]').first();
      await expect(composer).toBeVisible({ timeout: 30000 });
      await composer.fill(userMessage);
      await composer.press('Enter');
      await expect(page.getByText(assistantMessage, { exact: true }).first()).toBeVisible({ timeout: 120000 });
      sessionId = await page.locator('[data-conversation-session]').first().getAttribute('data-conversation-session');
      assert.ok(sessionId, 'The retained conversation must have a native Session identity');
    } else {
      const row = page.locator(`[data-row-key=${JSON.stringify(`session:${sessionId}`)}]`);
      await expect(row).toBeVisible({ timeout: 90000 });
      await row.locator(':scope > span').nth(1).click();
    }
    const conversation = page.locator(`[data-conversation-session=${JSON.stringify(sessionId)}]`);
    await expect(conversation.getByText(userMessage, { exact: true })).toBeVisible({ timeout: 90000 });
    await expect(conversation.getByText(assistantMessage, { exact: true })).toBeVisible({ timeout: 90000 });
    assert.deepEqual(errors, [], 'Native renderer must not report uncaught errors');
    report.launches.push({ restart: restart + 1, runtime, elapsedMs: Date.now() - started,
      nativeSession: true, userMessageRetained: true, assistantReplyRetained: true, rendererErrors: errors.length });
    await page.screenshot({ path: output.replace(/\.json$/, `-${restart + 1}.png`), fullPage: true });
    await app.close(); app = undefined; page = undefined;
  }
  report.defaultNativeDataDirectories = true;
  report.modelConfigurationRetained = true;
  report.passed = true;
} catch (error) {
  report.failure = { message: String(error.message).split('\n')[0].slice(0, 250), completedLaunches: report.launches.length };
  await page?.screenshot({ path: output.replace(/\.json$/, '-failure.png'), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await app?.close().catch(() => {});
  server.closeAllConnections(); await new Promise(done => server.close(done));
  await writeFile(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
}
