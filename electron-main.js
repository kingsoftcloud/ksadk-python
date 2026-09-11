const {app, BrowserWindow, dialog, Menu, nativeImage, ipcMain} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const {startRuntime, requestJson} = require('./desktop-runtime');
let runtime;
let window;
let quitting = false;
app.setName('AgentKit Studio');
const partition = 'studio-' + process.pid;
const resources = path.resolve(__dirname, '..');

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
async function switchWorkspace() {
  const workspace = await chooseWorkspaceForSwitch();
  if (!workspace || !runtime) return;
  try {
    const result = await requestJson(runtime.port, '/api/v1/workspaces:open', {
      method: 'POST', data: {path: workspace}, cookie: runtime.cookie, csrf: runtime.csrf,
    });
    if (result.status !== 200) throw new Error(result.body?.message || '切换工作区失败');
    saveWorkspace(workspace);
    window.reload();
  } catch (error) { dialog.showErrorBox('切换工作区失败', error.message); }
}
async function launch() {
  const icon = nativeImage.createFromPath(path.join(resources, 'AgentKitStudio.icns'));
  if (!icon.isEmpty() && app.dock) app.dock.setIcon(icon);
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label: 'AgentKit Studio', submenu: [{role: 'about'}, {type: 'separator'}, {role: 'quit'}]},
    {label: '工作区', submenu: [{label: '打开工作区…', accelerator: 'CmdOrCtrl+O', click: switchWorkspace}]},
    {role: 'editMenu'}, {role: 'viewMenu'}, {role: 'windowMenu'},
  ]));
  const workspace = await chooseWorkspace();
  const logPath = path.join(app.getPath('logs'), 'studio-backend.log');
  runtime = await startRuntime({resources, workspace, logPath, preferredPort: Number(process.env.STUDIO_APP_PORT || 0)});
  if (workspace !== defaultWorkspace()) saveWorkspace(workspace);
  runtime.child.on('exit', () => {
    if (!quitting) {
      dialog.showErrorBox('Studio 后端已退出', '请重新打开应用。诊断日志：' + logPath);
      app.quit();
    }
  });
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
    if (new URL(url).origin !== new URL(runtime.url).origin) event.preventDefault();
  });
  await window.loadURL(runtime.url);
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { window.show(); window.focus(); } });
  app.whenReady().then(launch).catch(error => { dialog.showErrorBox('AgentKit Studio 启动失败', error.message); app.quit(); });
}
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { quitting = true; if (runtime) runtime.child.kill('SIGTERM'); });
