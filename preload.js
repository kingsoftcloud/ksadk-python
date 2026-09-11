const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('studioNative', {
  chooseWorkspace: () => ipcRenderer.invoke('studio:choose-workspace'),
  openWorkspace: () => ipcRenderer.invoke('studio:open-workspace'),
});
