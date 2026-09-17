const {contextBridge, ipcRenderer} = require('electron');

function onChannel(name, callback) {
  const listener = (_event, payload) => callback(payload);
  ipcRenderer.on(name, listener);
  return () => ipcRenderer.removeListener(name, listener);
}

contextBridge.exposeInMainWorld('studioNative', {
  chooseWorkspace: () => ipcRenderer.invoke('studio:choose-workspace'),
  openWorkspace: () => ipcRenderer.invoke('studio:open-workspace'),
  onWorkspaceOpened: callback => onChannel('studio:workspace-opened', callback),
  // Auto-update: the renderer can trigger a manual check, observe update
  // lifecycle events, and (once downloaded) request install-on-quit.
  checkForUpdates: () => ipcRenderer.invoke('studio:check-for-updates'),
  quitAndInstall: () => ipcRenderer.invoke('studio:quit-and-install'),
  onUpdateAvailable: callback => onChannel('studio:update-available', callback),
  onUpdateNotAvailable: callback => onChannel('studio:update-not-available', callback),
  onUpdateProgress: callback => onChannel('studio:update-progress', callback),
  onUpdateDownloaded: callback => onChannel('studio:update-downloaded', callback),
  onUpdateError: callback => onChannel('studio:update-error', callback),
});
