/** Disposable-runner qualification: real signed bundles and native Squirrel install. No publication. */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, mkdtemp, readFile, readdir, realpath, stat, writeFile, rm } from 'node:fs/promises';
import { createServer as httpServer } from 'node:http';
import { createServer as httpsServer } from 'node:https';
import { homedir, tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';

assert.equal(process.platform, 'darwin');
assert.equal(process.env.GITHUB_ACTIONS, 'true', 'Trust and native profile qualification are restricted to disposable CI');
assert.ok(process.env.RUNNER_TEMP);
const execWithTimeout = promisify(execFile);
const exec = (command, args, options = {}) => execWithTimeout(command, args, { timeout: 120000, ...options });
const tools = resolve(process.env.STUDIO_TOOLS_ROOT);
const require = createRequire(join(tools, 'package.json'));
const { _electron, expect } = require('@playwright/test');
const asar = require('@electron/asar');
const YAML = require('yaml');
const { installedStudioFingerprint } = await import(join(tools, 'scripts/installed-studio-fingerprint.mjs'));
const { createBuildOutputRedactor } = await import(join(tools, 'scripts/build-output-redaction.mjs'));
const evidence = resolve(process.env.STUDIO_INSTALL_EVIDENCE);
await mkdir(evidence, { recursive: true, mode: 0o700 });
const scratch = await mkdtemp(join(tmpdir(), 'studio-installed-native-'));
const install = join(scratch, 'AgentKit Studio.app');
const marker = 'NATIVE_UPDATE_CONTEXT_' + randomUUID().replaceAll('-', '');
const report = { schemaVersion: 1, passed: false, controlledDelivery: true, published: false,
  realElectronUpdater: true, model: 'local synthetic provider; real external provider qualified separately',
  sourceCommit: '06bfe614775efccf30faefa2cb9a027b96ea084e',
  baselineVersion: '0.2.0-rc.2.20261014.10', targetVersion: '0.2.0-rc.2.20261015.38',
  checkpoints: [], modelRequests: [], transfers: [], limits: ['Controlled HTTPS transport; production feed is not modified or qualified by this run.'] };
const reportFile = join(evidence, 'installed-update-controlled.json');
const nativeObserver = join(scratch, 'observe-native-window');
const checkpoint = async stage => {
  report.stage = stage; report.checkpoints.push({ stage, time: new Date().toISOString() });
  await writeFile(reportFile, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({ stage, passed: report.passed }));
};
const sleep = ms => new Promise(done => setTimeout(done, ms));
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
async function shaFile(file, algorithm = 'sha256', encoding = 'hex') {
  const hash = createHash(algorithm); for await (const bytes of createReadStream(file)) hash.update(bytes); return hash.digest(encoding);
}
async function walk(root) {
  const files = [];
  for (const entry of await readdir(root, { withFileTypes: true }).catch(() => [])) {
    const path = join(root, entry.name); if (entry.isDirectory()) files.push(...await walk(path)); else if (entry.isFile()) files.push(path);
  } return files;
}
async function version() {
  asar.uncacheAll();
  const archive = join(install, 'Contents/Resources/app.asar');
  const manifest = JSON.parse(asar.extractFile(archive, 'package.json'));
  const plist = (await exec('/usr/bin/plutil', ['-extract', 'CFBundleShortVersionString', 'raw', '-o', '-', join(install, 'Contents/Info.plist')])).stdout.trim();
  assert.equal(manifest.version, plist);
  return { version: plist, ...installedStudioFingerprint(archive) };
}
async function processIds() {
  const executable = join(install, 'Contents/MacOS/AgentKit Studio');
  const paths = new Set([executable, await realpath(executable)]);
  const lines = (await exec('/bin/ps', ['-ax', '-o', 'pid=,command='])).stdout.split('\n');
  return lines.flatMap(line => { const m = line.trim().match(/^(\d+)\s+(.+)$/); return m && [...paths].some(path => m[2] === path || m[2].startsWith(path + ' ')) ? [Number(m[1])] : []; });
}
function alive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}
async function journalEvents() {
  const files = await walk(join(evidence, 'journals'));
  return (await Promise.all(files.map(file => readFile(file, 'utf8'))))
    .flatMap(text => text.trim().split('\n').filter(Boolean).map(line => JSON.parse(line)));
}
async function until(name, operation, timeout = 180000) {
  const end = Date.now() + timeout; let last;
  while (Date.now() < end) { try { const result = await operation(); if (result) return result; } catch (error) { last = error; } await sleep(1000); }
  throw new Error(`${name} timed out${last ? ': ' + String(last.message).split('\n')[0].slice(0, 160) : ''}`);
}
let electron, electronProcess, model, delivery, trustedFingerprint, initialPid, nativeSession, retained = [];
const ownedMainPids = new Set();
const nativeHome = join(homedir(), '.dsh');
const productHome = join(homedir(), '.agentkit');
async function observeNativeWindow(pid, name) {
  const output = join(evidence, name + '.json');
  await exec(nativeObserver, [String(pid), install, join(evidence, name + '.png'), output, marker], { timeout: 45000 });
  const observation = JSON.parse(await readFile(output));
  assert.equal(observation.retainedSessionMarkerVisible, true);
  assert.equal(observation.successorLaunchedByObserver, false);
  return observation;
}
try {
  await checkpoint('checking-disposable-native-data');
  await exec('xcrun', ['swiftc', join(import.meta.dirname, 'observe-studio-native-window.swift'), '-o', nativeObserver], { timeout: 180000 });
  for (const path of [nativeHome, productHome]) {
    assert.equal(await stat(path).then(() => true, error => { if (error.code === 'ENOENT') return false; throw error; }), false, 'Native qualification requires an unused runner profile');
  }
  const baseline = resolve(process.env.STUDIO_BASELINE_DMG);
  assert.equal(await shaFile(baseline), '7aa7accacd1382c2443d614c90140fe866ad4592d4d814417db9530b86c769f2');
  const candidate = resolve(process.env.STUDIO_CANDIDATE_DIR);
  const feed = YAML.parse(await readFile(join(candidate, 'preview-mac.yml'), 'utf8'));
  assert.equal(feed.version, report.targetVersion);
  const zipName = basename(feed.files[0].url); const zip = join(candidate, zipName);
  assert.equal(await shaFile(zip, 'sha512', 'base64'), feed.files[0].sha512);
  assert.equal((await stat(zip)).size, feed.files[0].size);
  const mount = join(scratch, 'mount'); await mkdir(mount);
  await exec('/usr/bin/hdiutil', ['attach', '-nobrowse', '-readonly', '-mountpoint', mount, baseline]);
  try { await exec('/usr/bin/ditto', [join(mount, 'AgentKit Studio.app'), install]); }
  finally { await exec('/usr/bin/hdiutil', ['detach', mount, '-force']); }
  for (const [cmd, args] of [['/usr/bin/codesign', ['--verify', '--deep', '--strict', install]], ['/usr/sbin/spctl', ['--assess', '--type', 'execute', '--verbose=4', install]]]) await exec(cmd, args);
  report.before = await version(); assert.equal(report.before.version, report.baselineVersion);
  await checkpoint('preparing-controlled-https');
  const ca = join(scratch, 'delivery-ca.pem'), caKey = join(scratch, 'delivery-ca.key');
  const cert = join(scratch, 'delivery.pem'), key = join(scratch, 'delivery.key');
  await writeFile(join(scratch, 'ca-config'), '[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\n[dn]\n[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n');
  await exec('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', caKey, '-out', ca, '-days', '1', '-subj', '/CN=Studio Disposable Qualification CA', '-config', join(scratch, 'ca-config')]);
  await exec('openssl', ['req', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', join(scratch, 'delivery.csr'), '-subj', '/CN=localhost']);
  await writeFile(join(scratch, 'extensions'), 'subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n');
  await exec('openssl', ['x509', '-req', '-in', join(scratch, 'delivery.csr'), '-CA', ca, '-CAkey', caKey, '-CAcreateserial', '-out', cert, '-days', '1', '-extfile', join(scratch, 'extensions')]);
  trustedFingerprint = (await exec('openssl', ['x509', '-in', ca, '-noout', '-fingerprint', '-sha1'])).stdout.trim().split('=').at(-1).replaceAll(':', '');
  await exec('sudo', ['security', 'add-trusted-cert', '-d', '-r', 'trustRoot', '-k', '/Library/Keychains/System.keychain', ca]);
  delivery = httpsServer({ key: await readFile(key), cert: await readFile(cert) }, (req, res) => {
    if (req.url?.split('?')[0] === '/preview-mac.yml') { res.setHeader('content-type', 'application/yaml'); res.end(YAML.stringify(feed)); return; }
    if (req.url?.split('?')[0] !== '/' + zipName) { res.writeHead(404); res.end(); return; }
    const size = feed.files[0].size, range = req.headers.range?.match(/^bytes=(\d+)-(\d*)$/);
    let start = 0, end = size - 1;
    if (req.headers.range && !range) { res.writeHead(416); res.end(); return; }
    if (range) { start = Number(range[1]); end = range[2] ? Number(range[2]) : end; }
    if (start > end || end >= size) { res.writeHead(416); res.end(); return; }
    res.setHeader('content-type', 'application/zip'); res.setHeader('accept-ranges', 'bytes'); res.setHeader('content-length', end - start + 1);
    if (range) res.writeHead(206, { 'content-range': `bytes ${start}-${end}/${size}` });
    report.transfers.push({ asset: zipName, start, end, bytes: end - start + 1 }); createReadStream(zip, { start, end }).pipe(res);
  });
  await new Promise(done => delivery.listen(0, '127.0.0.1', done));
  const deliveryOrigin = `https://127.0.0.1:${delivery.address().port}/`;
  model = httpServer(async (req, res) => {
    const chunks = []; for await (const bytes of req) chunks.push(bytes);
    if (req.url === '/v1/models') { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ object: 'list', data: [{ id: 'native-retention', object: 'model' }] })); return; }
    if (req.url !== '/v1/chat/completions') { res.writeHead(404); res.end(); return; }
    const body = JSON.parse(Buffer.concat(chunks)); const messages = body.messages || [];
    const userMessages = messages.filter(message => message.role === 'user');
    const continuationTurn = userMessages.some(message => JSON.stringify(message.content).includes('Turn 2.')) ? 2 : undefined;
    const priorAssistantMarker = messages.some(message => message.role === 'assistant' && JSON.stringify(message.content).includes(marker));
    // Native request context can contain additional user-role environment messages;
    // that count is not a conversation turn. Validate retained assistant context separately.
    report.modelRequests.push({ userMessages: userMessages.length, continuationTurn, priorAssistantMarker, nativeModel: body.model === 'native-retention' });
    res.setHeader('content-type', 'text/event-stream');
    const id = 'chatcmpl-native-local';
    for (const delta of [{ role: 'assistant', content: marker }, {}]) res.write('data: ' + JSON.stringify({ id, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: body.model, choices: [{ index: 0, delta, finish_reason: Object.keys(delta).length ? null : 'stop' }] }) + '\n\n');
    res.end('data: [DONE]\n\n');
  });
  await new Promise(done => model.listen(0, '127.0.0.1', done));
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/(KEY|SECRET|TOKEN|PASSWORD|CERTUM|APPLE|CSC_|^DSH_HOME$|^AGENTKIT_HOME$|^NODE_OPTIONS$|^ELECTRON_RUN_AS_NODE$)/i.test(name)));
  env.DSH_DESKTOP_UPDATE_JOURNAL_DIR = join(evidence, 'journals'); env.DSH_TELEMETRY_DISABLED = '1';
  await exec('/bin/launchctl', ['setenv', 'DSH_DESKTOP_UPDATE_JOURNAL_DIR', env.DSH_DESKTOP_UPDATE_JOURNAL_DIR]);
  await checkpoint('launching-real-signed-baseline');
  electron = await _electron.launch({ executablePath: join(install, 'Contents/MacOS/AgentKit Studio'), args: [], cwd: scratch, env, timeout: 120000 });
  electronProcess = electron.process();
  initialPid = electronProcess.pid;
  ownedMainPids.add(initialPid);
  assert.ok((await processIds()).includes(initialPid), 'Owned initial main process must be observable with its automation arguments');
  await electron.evaluate(({ app }) => { app.accessibilitySupportEnabled = true; });
  for (const stream of [electronProcess.stdout, electronProcess.stderr]) stream?.pipe(createBuildOutputRedactor()).pipe(process.stdout, { end: false });
  const page = await electron.firstWindow({ timeout: 120000 }); page.setDefaultTimeout(30000);
  const guide = page.locator('.agentkit-credential-onboarding-content'); await expect(guide).toBeVisible({ timeout: 120000 });
  await guide.getByRole('button', { name: '开始设置', exact: true }).click();
  const form = guide.locator('[data-onboarding-mode=model]');
  await expect(form.getByRole('button', { name: '保存并继续', exact: true })).toBeEnabled({ timeout: 90000 });
  await form.getByRole('combobox', { name: /^提供商/ }).selectOption('custom');
  await form.getByLabel('模型名称', { exact: true }).fill('native-retention');
  await form.getByLabel('API 地址', { exact: true }).fill(`http://127.0.0.1:${model.address().port}/v1`);
  await form.getByLabel('API Key', { exact: true }).fill('local-native-qualification-placeholder');
  await form.getByRole('button', { name: '保存并继续', exact: true }).click();
  await guide.getByRole('button', { name: '跳过，开始对话', exact: true }).click();
  await expect(guide).toBeHidden();
  await expect(page.getByRole('button', { name: 'Agents', exact: true })).toBeVisible();
  await checkpoint('creating-native-session-in-localized-workspace');
  const workspace = page.getByRole('treeitem').filter({ has: page.getByText(/^(默认工作区|Default workspace)$/) });
  await workspace.hover(); await workspace.getByRole('button', { name: /在“.*”中新建会话|^New session in / }).click();
  const composer = page.locator('[contenteditable="true"]').first(); await composer.fill('Remember a unique context marker. Reply only with the marker. Do not use tools.'); await composer.press('Enter');
  await expect(page.locator('[data-conversation-session]').first().getByText(marker, { exact: true })).toBeVisible({ timeout: 180000 });
  await expect(page.getByRole('button', { name: /^(停止生成|Stop generating)$/ })).toHaveCount(0, { timeout: 180000 });
  nativeSession = await page.locator('[data-conversation-session]').first().getAttribute('data-conversation-session'); assert.ok(nativeSession);
  await page.screenshot({ path: join(evidence, 'baseline-session.png'), fullPage: true });
  await checkpoint('observing-existing-native-baseline-window');
  report.baselineVisibility = await electron.evaluate(({ app, BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows().find(window => window.webContents.getURL().startsWith('dsh-app://app/'));
    if (!window) throw Error('Owned native workspace window is missing');
    window.show(); window.focus(); app.focus({ steal: true });
    return { pid: process.pid, windowId: window.id, visible: window.isVisible(), minimized: window.isMinimized() };
  });
  report.baselineWindow = await observeNativeWindow(initialPid, 'baseline-native-window');
  await checkpoint('recording-existing-native-data');
  const files = [...await walk(nativeHome), ...await walk(productHome)];
  const selected = files.filter(path => path.includes(nativeSession) && /session\.v\d+\.jsonl(?:\.zstd)?$/.test(path) || path === join(productHome, 'config.yaml'));
  assert.ok(selected.some(path => path.includes(nativeSession))); assert.ok(selected.includes(join(productHome, 'config.yaml')));
  retained = await Promise.all(selected.map(async path => ({ path, bytes: await readFile(path) })));
  report.retainedFiles = retained.map(item => ({ relativePath: item.path.replace(homedir(), '~'), bytesBefore: item.bytes.length, sha256Before: digest(item.bytes) }));
  await electron.evaluate(({ app }, url) => {
    const require = process.getBuiltinModule('module').createRequire(app.getAppPath() + '/package.json');
    require('electron-updater').autoUpdater.setFeedURL({ provider: 'generic', url, channel: 'preview', useMultipleRangeRequest: false });
  }, deliveryOrigin);
  await checkpoint('checking-and-confirming-real-update');
  await electron.evaluate(({ Menu }) => {
    const find = menu => {
      for (const item of menu?.items || []) {
        if (/^(检查更新|Check for [Uu]pdates)(?:…|\.\.\.)?$/.test(item.label)) return item;
        const child = find(item.submenu); if (child) return child;
      }
    };
    const item = find(Menu.getApplicationMenu());
    if (!item?.enabled) throw new Error('Native application update menu is unavailable');
    item.click(item);
  });
  report.checkTrigger = 'actual native application menu callback; native download and restart dialog buttons clicked';
  const dialog = await until('native update download prompt', async () => electron.windows().find(window => window.url().includes('shell/update-dialog.html')));
  dialog.setDefaultTimeout(30000);
  await expect(dialog.getByText(report.targetVersion, { exact: false }).first()).toBeVisible({ timeout: 90000 });
  await dialog.screenshot({ path: join(evidence, 'download-confirmation.png') });
  await dialog.getByRole('button', { name: /^(下载更新|Download update)$/ }).click();
  // Completing the download closes its confirmation document. Installation
  // owns a newly created shell dialog, not the page that authorized download.
  const installationDialog = await until('separate native installation confirmation', async () => {
    for (const window of electron.windows()) {
      if (!window.isClosed() && window.url().includes('shell/update-dialog.html')
          && await window.getByRole('button', { name: /^(安装并重启|Install and Restart)$/ }).isVisible()) return window;
    }
    return false;
  }, 300000);
  await installationDialog.screenshot({ path: join(evidence, 'install-confirmation.png') });
  await checkpoint('installing-through-native-squirrel');
  await installationDialog.getByRole('button', { name: /^(安装并重启|Install and Restart)$/ }).click();
  await until('actual bundle version replacement', async () => (await version()).version === report.targetVersion, 300000);
  await until('native installer requested the original application exit', async () =>
    (await journalEvents()).some(event => event.pid === initialPid && event.event === 'quit-requested'));
  // Playwright's main-process debugger otherwise holds Node at "Waiting for
  // the debugger to disconnect" after the native updater has requested quit.
  // Its public close API detaches that debugger. Only do this after the native
  // installer has replaced the bundle and the original App has requested quit.
  const initialProcess = electronProcess;
  await checkpoint('detaching-automation-after-native-quit-request');
  let detachTimer;
  await Promise.race([electron.close(), new Promise((_, reject) => { detachTimer = setTimeout(() => reject(Error('Native quit debugger detach deadline')), 30000); })])
    .finally(() => clearTimeout(detachTimer));
  electron = undefined;
  report.debuggerDetachedAfterNativeQuitRequested = true;
  const successorReadiness = await until('automatic native successor', async () => {
    if (initialProcess.exitCode === null && initialProcess.signalCode === null) return false;
    const events = await journalEvents();
    const ready = events.find(event => event.version === report.targetVersion && event.event === 'workspace-ready'
      && event.pid !== initialPid && alive(event.pid));
    return ready || false;
  });
  const successorPid = successorReadiness.pid;
  ownedMainPids.add(successorPid);
  report.successorReadiness = successorReadiness;
  report.successorPid = successorPid; report.originalPidExited = true; report.successorLaunchedByHarness = false;
  report.after = await version();
  assert.equal(report.after.studioSourceSha256, 'a930296e5caab1eba7adf093f80be6724295da0edee15a19dadab67577a68b56');
  assert.equal(report.after.studioPackageSha256, '9de0dc32673eb1fe586c25436557d66050e4b35a32d39b33772f132c509783da');
  for (const [cmd, args] of [['/usr/bin/codesign', ['--verify', '--deep', '--strict', install]], ['/usr/sbin/spctl', ['--assess', '--type', 'execute', '--verbose=4', install]]]) await exec(cmd, args);
  for (let i = 0; i < retained.length; i++) {
    const current = await readFile(retained[i].path); assert.ok(current.subarray(0, retained[i].bytes.length).equals(retained[i].bytes)); report.retainedFiles[i].originalBytesRetained = true;
  }
  await checkpoint('observing-actual-successor-native-ui');
  report.successorWindow = await observeNativeWindow(successorPid, 'successor-native-window');
  const events = await journalEvents();
  for (const event of ['started', 'check-requested', 'download-requested', 'install-confirmed']) assert.ok(events.some(item => item.event === event));
  assert.ok(events.some(item => item.version === report.targetVersion && item.pid === successorPid && item.event === 'workspace-ready'));
  assert.ok(report.transfers.some(item => item.bytes === feed.files[0].size), 'A fresh DMG profile must actually download the signed ZIP');
  // The existing automatic successor has already been independently observed.
  // A separate cold restart then verifies native Session use through visible UI.
  await checkpoint('cold-restarting-observed-updated-app-for-session-continuation');
  await exec('/bin/kill', ['-TERM', String(successorPid)]);
  await until('observed automatic successor exits normally', async () => !alive(successorPid), 30000);
  electron = await _electron.launch({ executablePath: join(install, 'Contents/MacOS/AgentKit Studio'), args: [], cwd: scratch, env, timeout: 120000 });
  electronProcess = electron.process();
  ownedMainPids.add(electronProcess.pid);
  for (const stream of [electronProcess.stdout, electronProcess.stderr]) stream?.pipe(createBuildOutputRedactor()).pipe(process.stdout, { end: false });
  const continued = await electron.firstWindow({ timeout: 120000 }); continued.setDefaultTimeout(30000);
  assert.equal(await electron.evaluate(({ app }) => app.getVersion()), report.targetVersion);
  const row = continued.locator('[data-row-key=' + JSON.stringify('session:' + nativeSession) + ']');
  await expect(row).toBeVisible({ timeout: 90000 }); await row.locator(':scope > span').nth(1).click();
  const conversation = continued.locator('[data-conversation-session=' + JSON.stringify(nativeSession) + ']');
  await expect(conversation.getByText(marker, { exact: true })).toHaveCount(1);
  const nextComposer = continued.locator('[contenteditable="true"]').first();
  await nextComposer.fill('Repeat the context marker from the earlier assistant response. Reply only with that marker. Turn 2. Do not use tools.');
  await nextComposer.press('Enter');
  await expect(conversation.getByText(marker, { exact: true })).toHaveCount(2, { timeout: 180000 });
  await expect(continued.getByRole('button', { name: /^(停止生成|Stop generating)$/ })).toHaveCount(0, { timeout: 180000 });
  assert.ok(report.modelRequests.some(item => item.continuationTurn === 2 && item.priorAssistantMarker));
  await continued.screenshot({ path: join(evidence, 'updated-native-session-continuation.png'), fullPage: true });
  report.sessionContinuationMethod = 'separate cold restart after independently observing the native automatic successor';
  report.passed = true; report.dataRetained = true; report.nativeContextContinuedAfterUpdate = true;
  await checkpoint('native-update-accepted-rollback-still-separate');
} catch (error) {
  report.failure = { stage: report.stage, name: error.name, message: String(error.message).split('\n')[0].slice(0, 500),
    detail: String(error.message).replace(/([?&]token=)[^\s"<>]+/g, '$1[redacted]').slice(0, 2000) };
  console.log(JSON.stringify({ failureStage: report.stage, errorName: error.name }));
  process.exitCode = 1;
  if (electron) for (const [index, page] of electron.windows().entries()) await page.screenshot({ path: join(evidence, `failure-${index}.png`), timeout: 10000 }).catch(() => {});
  await checkpoint('failed');
} finally {
  if (electron && electronProcess.exitCode === null && electronProcess.signalCode === null) {
    let timer;
    await Promise.race([electron.close(), new Promise((_, reject) => { timer = setTimeout(() => reject(Error('Baseline close deadline')), 30000); })])
      .catch(() => { report.cleanupFailure = true; process.exitCode = 1; electronProcess.kill('SIGKILL'); })
      .finally(() => clearTimeout(timer));
  }
  for (const pid of new Set([...ownedMainPids, ...await processIds()])) if (alive(pid)) await exec('/bin/kill', ['-TERM', String(pid)]).catch(() => {});
  await until('owned native processes exit', async () => [...ownedMainPids].every(pid => !alive(pid)) && (await processIds()).length === 0, 30000).catch(() => { report.cleanupFailure = true; process.exitCode = 1; });
  for (const server of [model, delivery]) if (server) { server.closeAllConnections(); await new Promise(done => server.close(done)); }
  await exec('/bin/launchctl', ['unsetenv', 'DSH_DESKTOP_UPDATE_JOURNAL_DIR']).catch(() => { report.cleanupFailure = true; process.exitCode = 1; });
  if (trustedFingerprint) await exec('sudo', ['security', 'delete-certificate', '-Z', trustedFingerprint, '/Library/Keychains/System.keychain']).catch(() => { report.cleanupFailure = true; process.exitCode = 1; });
  for (const name of ['delivery-ca.key', 'delivery.key']) await rm(join(scratch, name), { force: true });
  if (report.cleanupFailure) report.passed = false;
  await writeFile(reportFile, JSON.stringify(report, null, 2) + '\n');
}
