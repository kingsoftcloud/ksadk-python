const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('studioNative', {
  chooseWorkspace: () => ipcRenderer.invoke('studio:choose-workspace'),
  openWorkspace: () => ipcRenderer.invoke('studio:open-workspace'),
  onWorkspaceOpened: callback => {
    const listener = (_event, workspace) => callback(workspace);
    ipcRenderer.on('studio:workspace-opened', listener);
    return () => ipcRenderer.removeListener('studio:workspace-opened', listener);
  },
});
