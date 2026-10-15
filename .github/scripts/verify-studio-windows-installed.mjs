/** Verify the real NSIS-installed app and retained conversation on a disposable Runner. */
import assert from 'node:assert/strict';
import { _electron, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, normalize, resolve } from 'node:path';
import { parseArgs, promisify } from 'node:util';
import { execFile } from 'node:child_process';
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
let modelRequests = 0;
const server = createServer((request, response) => {
  request.resume();
  if (request.url === '/v1/models') {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ object: 'list', data: [{ id: 'native-proof', object: 'model' }] }));
    return;
  }
  modelRequests++;
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
  limits: ['Signature and updater acceptance require separate evidence.', 'Loopback provider proves native Host integration and retention, not a real external model.'] };
let app, page, sessionId;
async function checkpoint(stage) {
  Object.assign(report, { stage, observedAt: new Date().toISOString(), modelRequests });
  await writeFile(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({ stage, completedLaunches: report.launches.length, modelRequests, passed: report.passed }));
}
async function closeApplication() {
  if (!app) return;
  const application = app;
  app = undefined;
  const ownedPid = application.process().pid;
  let timer;
  const closed = await Promise.race([
    application.close().then(() => true, () => false),
    new Promise(done => { timer = setTimeout(() => done(false), 20000); }),
  ]).finally(() => clearTimeout(timer));
  if (!closed) {
    report.passed = false;
    report.forcedCleanup = true;
    process.exitCode = 1;
    await checkpoint('native-close-failed');
    await promisify(execFile)('taskkill.exe', ['/PID', String(ownedPid), '/T', '/F'], { timeout: 15000 }).catch(() => {
      report.cleanupFailed = true;
    });
    throw new Error('Owned native app did not close normally; forced cleanup is an acceptance failure');
  }
}
try {
  await checkpoint('checking-installed-archive');
  const archive = join(dirname(executablePath), 'resources/app.asar');
  report.packagedFiles = {};
  for (const file of ['src/index.mjs', 'dist/client.js']) {
    const bytes = asar.extractFile(archive, normalize(`dsh/node_modules/@kingsoftcloud/agentkit-studio/${file}`));
    assert.ok(bytes.equals(await readFile(file)), `Installed ${file} must match the verified source`);
    report.packagedFiles[file] = createHash('sha256').update(bytes).digest('hex');
  }
  for (let restart = 0; restart < 2; restart++) {
    const errors = [], started = Date.now();
    await checkpoint(restart ? 'restarting-native-app' : 'launching-native-app');
    app = await _electron.launch({ executablePath, cwd: dirname(executablePath), env: environment, timeout: 120000 });
    page = await app.firstWindow({ timeout: 120000 });
    page.setDefaultTimeout(30000);
    page.setDefaultNavigationTimeout(30000);
    page.on('pageerror', error => errors.push(error.name));
    await expect(page.getByRole('button', { name: 'Agents', exact: true })).toBeVisible({ timeout: 120000 });
    const runtime = await app.evaluate(({ app }) => ({ version: app.getVersion(), isPackaged: app.isPackaged, node: process.versions.node, electron: process.versions.electron }));
    assert.equal(runtime.isPackaged, true);
    assert.equal(runtime.version, values.version);
    await checkpoint('native-window-ready');
    if (restart === 0) {
      const guide = page.locator('.agentkit-credential-onboarding-content');
      await expect(guide).toBeVisible({ timeout: 120000 });
      await guide.getByRole('button', { name: /^(开始设置|去配置模型凭证)$/ }).click();
      const models = page.locator('[data-studio-model-settings]');
      await expect(models.getByRole('button', { name: '保存并继续', exact: true })).toBeEnabled({ timeout: 90000 });
      await models.getByRole('combobox', { name: /^提供商/ }).first().selectOption('custom');
      await models.getByLabel('模型名称', { exact: true }).first().fill('native-proof');
      await models.getByLabel('API 地址', { exact: true }).first().fill(`http://127.0.0.1:${server.address().port}/v1`);
      await models.getByLabel(/^API Key/).first().fill('fixture-native-only');
      await checkpoint('saving-native-model-configuration');
      await models.getByRole('button', { name: '保存并继续', exact: true }).click();
      await expect(guide).toHaveAttribute('data-studio-onboarding-stage', 'cloud', { timeout: 90000 });
      await guide.getByRole('button', { name: '跳过，开始对话', exact: true }).click();
      await expect(guide).toBeHidden({ timeout: 30000 });
      await checkpoint('native-guide-completed');
      await page.getByRole('treeitem').filter({ hasText: /Default workspace|默认工作区/ }).first().hover();
      await page.getByRole('button', { name: /New session in|中新建会话/ }).first().click();
      const composer = page.locator('[contenteditable="true"]').first();
      await expect(composer).toBeVisible({ timeout: 30000 });
      await composer.fill(userMessage);
      await composer.press('Enter');
      await expect(page.getByText(assistantMessage, { exact: true }).first()).toBeVisible({ timeout: 120000 });
      sessionId = await page.locator('[data-conversation-session]').first().getAttribute('data-conversation-session');
      assert.ok(sessionId, 'The retained conversation must have a native Session identity');
      await checkpoint('native-first-reply-created');
    } else {
      const row = page.locator(`[data-row-key=${JSON.stringify(`session:${sessionId}`)}]`);
      await expect(row).toBeVisible({ timeout: 90000 });
      await row.locator(':scope > span').nth(1).click();
    }
    const conversation = page.locator(`[data-conversation-session=${JSON.stringify(sessionId)}]`);
    await expect(conversation.getByText(userMessage, { exact: true })).toBeVisible({ timeout: 90000 });
    await expect(conversation.getByText(assistantMessage, { exact: true })).toBeVisible({ timeout: 90000 });
    if (restart === 1) {
      const settings = await page.evaluate(async expectedEndpoint => {
        const response = await fetch('/agentkit', { method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ method: 'settings/get', payload: {}, rpcId: 'native-restart-model-proof' }) });
        const result = (await response.json()).result;
        return { status: response.status, ok: result?.ok === true,
          configured: result?.value?.modelProfiles?.some(profile => profile.modelName === 'native-proof' && profile.credentialConfigured === true
            && profile.baseUrl === expectedEndpoint && profile.default === true && profile.enabled !== false) };
      }, `http://127.0.0.1:${server.address().port}/v1`);
      assert.deepEqual(settings, { status: 200, ok: true, configured: true });
      await checkpoint('native-session-and-settings-retained');
      const requestsBefore = modelRequests;
      const composer = page.locator('[contenteditable="true"]').first();
      const continuation = 'Windows refactor continuation after the native restart.';
      await composer.fill(continuation); await composer.press('Enter');
      await expect(conversation.getByText(continuation, { exact: true })).toBeVisible({ timeout: 90000 });
      await expect(conversation.getByText(assistantMessage, { exact: true })).toHaveCount(2, { timeout: 120000 });
      assert.ok(modelRequests > requestsBefore, 'The restarted native Host must execute a new provider request');
      report.modelConfigurationRetained = true;
      report.continuationAfterRestart = true;
      await checkpoint('native-continuation-verified');
    }
    assert.deepEqual(errors, [], 'Native renderer must not report uncaught errors');
    report.launches.push({ restart: restart + 1, runtime, elapsedMs: Date.now() - started,
      nativeSession: true, userMessageRetained: true, assistantReplyRetained: true, rendererErrors: errors.length });
    await page.screenshot({ path: output.replace(/\.json$/, `-${restart + 1}.png`), fullPage: true });
    await checkpoint('closing-native-app');
    await closeApplication(); page = undefined;
  }
  report.defaultNativeDataDirectories = true;
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = { message: String(error.message).split('\n')[0].slice(0, 250), completedLaunches: report.launches.length };
  await checkpoint('native-acceptance-failed');
  await page?.screenshot({ path: output.replace(/\.json$/, '-failure.png'), fullPage: true, timeout: 15000 }).catch(() => {});
  process.exitCode = 1;
} finally {
  await closeApplication().catch(error => { report.cleanupFailure = String(error.message).split('\n')[0]; });
  server.closeAllConnections(); await new Promise(done => server.close(done));
  await checkpoint(report.passed ? 'complete' : 'failed');
  console.log(JSON.stringify(report));
}
