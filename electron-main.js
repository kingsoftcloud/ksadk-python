const {app, BrowserWindow, dialog, Menu, nativeImage, ipcMain} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const {startRuntime, requestJson} = require('./desktop-runtime');
// electron-updater is an optional runtime dependency bundled into the app.
// Require lazily so a missing/failed install never breaks app launch; the
// user simply gets no auto-update instead of a hard crash.
let autoUpdater = null;
try {
  ({autoUpdater} = require('electron-updater'));
} catch (error) {
  console.error('[updater] electron-updater unavailable, auto-update disabled:', error.message);
}
let runtime;
let window;
let quitting = false;
let switching = false;
let updateDownloaded = false;
app.setName('AgentKit Studio');
const partition = 'studio-' + process.pid;
const resources = path.resolve(__dirname, '..');

// Inject bundled toolchain paths so Python/Node/DSH subprocesses find them.
// On Windows there is no launcher (AgentKitStudio.exe is renamed electron.exe).
// On macOS we used to use a shell launcher, but that set process.defaultApp
// which broke app.isPackaged (and thus electron-updater). Doing it in JS keeps
// the app a "packaged" Electron app on both platforms.
if (process.platform === 'win32') {
  process.env.AGENTENGINE_PLUGIN_TOOLCHAIN_HOME = path.join(resources, 'plugin-toolchains');
  process.env.PATH = path.join(resources, 'node') + ';' + (process.env.PATH || '');
} else if (process.platform === 'darwin') {
  process.env.AGENTENGINE_PLUGIN_TOOLCHAIN_HOME = path.join(resources, 'plugin-toolchains');
  process.env.PATH = path.join(resources, 'node', 'bin') + ':' + (process.env.PATH || '');
}

// Auto-update: check GitHub Release on launch, download silently in the
// background, install on quit. STUDIO_APP_UPDATE_REPO overrides the provider:
// a string URL switches to the generic provider (internal CDN/KS3), otherwise
// the default GitHub provider reads the public kingsoftcloud/ksadk-python
// release. We ship hand-rolled bundles (no electron-builder blockmaps), so
// disable differential download — full zip/exe each update.
function setupAutoUpdater() {
  if (!autoUpdater) return;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.disableDifferentialDownload = true;
  const override = process.env.STUDIO_APP_UPDATE_REPO;
  if (override) {
    // electron-updater resolves manifest URLs with new URL('latest-mac.yml', baseUrl);
    // a URL without a trailing slash drops the last path segment. Normalize.
    const url = override.endsWith('/') ? override : override + '/';
    autoUpdater.setFeedURL({provider: 'generic', url});
  } else {
    autoUpdater.setFeedURL({provider: 'github', owner: 'kingsoftcloud', repo: 'ksadk-python'});
  }
  autoUpdater.on('update-available', info => {
    console.log('[updater] update available:', info.version);
    if (window) window.webContents.send('studio:update-available', {version: info.version});
  });
  autoUpdater.on('update-not-available', () => {
    if (window) window.webContents.send('studio:update-not-available');
  });
  autoUpdater.on('download-progress', progress => {
    if (window) window.webContents.send('studio:update-progress', {percent: progress.percent});
  });
  autoUpdater.on('update-downloaded', event => {
    updateDownloaded = true;
    console.log('[updater] update downloaded:', event.version);
    if (window) window.webContents.send('studio:update-downloaded', {version: event.version});
  });
  autoUpdater.on('error', (error) => {
    console.error('[updater] error:', error?.message || error);
    if (window) window.webContents.send('studio:update-error', {message: error?.message || String(error)});
  });
}
ipcMain.handle('studio:check-for-updates', async () => {
  if (!autoUpdater) return {available: false, reason: 'disabled'};
  try {
    const result = await autoUpdater.checkForUpdates();
    return {available: Boolean(result?.updateInfo), version: result?.updateInfo?.version};
  } catch (error) {
    return {available: false, error: error?.message || String(error)};
  }
});
ipcMain.handle('studio:quit-and-install', () => {
  if (!autoUpdater || !updateDownloaded) return false;
  quitting = true;
  // Wait for the Python/Node child to fully release file handles before
  // installing, otherwise the NSIS installer (Windows) can't overwrite DLLs
  // and the update leaves the bundle half-replaced. 5s fallback in case the
  // child is hung and never emits exit.
  const install = () => autoUpdater.quitAndInstall();
  if (runtime) {
    let installed = false;
    const doInstall = () => { if (!installed) { installed = true; install(); } };
    runtime.child.once('exit', doInstall);
    runtime.child.kill('SIGTERM');
    setTimeout(doInstall, 5000);
  } else {
    setImmediate(install);
  }
  return true;
});

function preferencesPath() { return path.join(app.getPath('userData'), 'workspace.json'); }
function defaultWorkspace() { const root = path.join(app.getPath('home'), '.agentkit', 'studio-workspace'); fs.mkdirSync(root, {recursive: true}); return root; }
function saveWorkspace(workspace) {
  fs.mkdirSync(app.getPath('userData'), {recursive: true});
  fs.writeFileSync(preferencesPath(), JSON.stringify({version: 1, source: 'user-selection', workspace}, null, 2), {mode: 0o600});
}
function savedWorkspace() {
  try {
    const value = JSON.parse(fs.readFileSync(preferencesPath(), 'utf8'));
    if (value?.version !== 1 || value?.source !== 'user-selection' || typeof value.workspace !== 'string' || !value.workspace) return null;
    return fs.realpathSync(value.workspace);
  } catch { return null; }
}
async function chooseWorkspace() {
  const explicit = process.env.STUDIO_APP_WORKSPACE;
  if (explicit && explicit !== '.') return fs.realpathSync(explicit);
  return savedWorkspace() || defaultWorkspace();
}
async function chooseWorkspaceForSwitch() {
  const result = await dialog.showOpenDialog({properties: ['openDirectory', 'createDirectory'], title: '打开 AgentKit Studio 工作区', buttonLabel: '打开目录'});
  return result.canceled ? null : result.filePaths[0];
}
ipcMain.handle('studio:choose-workspace', chooseWorkspaceForSwitch);
function watchRuntime(owned) {
  owned.child.on('exit', () => {
    if (!quitting && runtime === owned) {
      dialog.showErrorBox('Studio 后端已退出', '请重新打开应用。诊断日志：' + path.join(app.getPath('logs'), 'studio-backend.log'));
      app.quit();
    }
  });
}
async function switchWorkspace() {
  if (switching || !runtime) return null;
  const workspace = await chooseWorkspaceForSwitch();
  if (!workspace || fs.realpathSync(workspace) === runtime.workspace) return null;
  switching = true;
  try {
    // WorkspaceRuntimeManager owns one service per directory in this Python
    // process. Reuse the supervised runtime instead of starting a second
    // Python/DSH stack; the current Studio session and cookie remain valid.
    const opened = await requestJson(runtime.port, '/api/v1/workspaces:open', {
      method: 'POST', data: {path: workspace, create: true},
      cookie: runtime.cookie, csrf: runtime.csrf, timeoutMs: 30000,
    });
    if (opened.status !== 200) {
      throw new Error(opened.body?.error?.message || `打开工作区失败（${opened.status}）`);
    }
    runtime.workspace = opened.body.path || fs.realpathSync(workspace);
    saveWorkspace(runtime.workspace);
    window.webContents.send('studio:workspace-opened', {path: runtime.workspace});
    return {path: runtime.workspace};
  } catch (error) {
    dialog.showErrorBox('切换工作区失败', error instanceof Error ? error.message : String(error));
    return null;
  } finally { switching = false; }
}
ipcMain.handle('studio:open-workspace', switchWorkspace);
function createWindow() {
  window = new BrowserWindow({width: 1440, height: 900, title: 'AgentKit Studio', webPreferences: {
    nodeIntegration: false, contextIsolation: true, sandbox: true, partition,
    preload: path.join(__dirname, 'preload.js'),
  }});
  window.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    console.error(`[renderer:${level}] ${message} (${sourceId}:${line})`);
  });
  window.webContents.on('did-fail-load', (_event, code, description, url) => {
    console.error(`[renderer:load-failed] ${code} ${description} ${url}`);
  });
  window.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  window.webContents.on('will-navigate', (event, url) => {
    if (runtime && new URL(url).origin !== new URL(runtime.url).origin) event.preventDefault();
  });
  return window;
}
async function showLoadingWindow() {
  await window.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html>
    <meta charset="utf-8"><style>html,body{height:100%;margin:0}body{display:grid;place-items:center;background:#f5f7fa;color:#1f2937;font:16px -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif}.card{text-align:center}.mark{margin:auto auto 18px;width:56px;height:56px;border-radius:16px;background:#1683e8;color:#fff;display:grid;place-items:center;font-size:30px;font-weight:700;box-shadow:0 8px 24px #1683e844}.hint{color:#64748b;margin-top:8px}</style>
    <main class="card"><div class="mark">K</div><strong>AgentKit Studio</strong><div class="hint">正在启动本地运行时…</div></main>`));
}
async function launch() {
  const iconPath = process.platform === 'win32'
    ? path.join(resources, 'AgentKitStudio.ico')
    : path.join(resources, 'AgentKitStudio.icns');
  const icon = nativeImage.createFromPath(iconPath);
  if (!icon.isEmpty() && app.dock) app.dock.setIcon(icon);
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label: 'AgentKit Studio', submenu: [
      {role: 'about'},
      {type: 'separator'},
      {label: '检查更新…', click: () => {
        if (!autoUpdater) { dialog.showErrorBox('自动更新', 'electron-updater 未安装,无法检查更新。'); return; }
        autoUpdater.checkForUpdates().catch(error => dialog.showErrorBox('检查更新失败', error?.message || String(error)));
      }},
      {type: 'separator'},
      {role: 'quit'},
    ]},
    {label: '工作区', submenu: [{label: '打开工作区…', accelerator: 'CmdOrCtrl+O', click: switchWorkspace}]},
    {role: 'editMenu'}, {role: 'viewMenu'}, {role: 'windowMenu'},
  ]));
  setupAutoUpdater();
  createWindow();
  await showLoadingWindow();
  const workspace = await chooseWorkspace();
  const logPath = path.join(app.getPath('logs'), 'studio-backend.log');
  runtime = await startRuntime({resources, workspace, logPath, preferredPort: Number(process.env.STUDIO_APP_PORT || 0)});
  if (workspace !== defaultWorkspace()) saveWorkspace(workspace);
  watchRuntime(runtime);
  await window.loadURL(runtime.url);
  // Silent background update check after the UI is up. Use checkForUpdates
  // (not checkForUpdatesAndNotify) so the renderer's own UI is the only
  // notification surface — the native OS notification would double up.
  if (autoUpdater) autoUpdater.checkForUpdates().catch(error => console.error('[updater] check failed:', error?.message || error));
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { window.show(); window.focus(); } });
  app.whenReady().then(launch).catch(error => { dialog.showErrorBox('AgentKit Studio 启动失败', error.message); app.quit(); });
}
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { quitting = true; if (runtime && !runtime.child.killed) runtime.child.kill('SIGTERM'); });
