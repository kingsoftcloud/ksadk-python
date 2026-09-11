const {app, BrowserWindow, dialog} = require('electron');
const {spawn} = require('child_process');
const http = require('http');
const path = require('path');
let service;
function waitForStudio(url, tries=120) {
  return new Promise((resolve,reject)=>{ const tick=()=>{ const req=http.get(url, r=>{ req.destroy(); resolve(); }); req.on('error',()=>{ if(--tries<=0) reject(new Error('Studio did not start')); else setTimeout(tick,250); }); }; tick(); });
}
async function chooseWorkspace() {
  const explicit = process.env.STUDIO_APP_WORKSPACE;
  if (explicit && explicit !== '.') return path.resolve(explicit);
  const result = await dialog.showOpenDialog({properties:['openDirectory','createDirectory'], title:'选择 AgentKit Studio 工作区', buttonLabel:'打开工作区'});
  if (result.canceled || !result.filePaths[0]) return null;
  return result.filePaths[0];
}
async function createWindow() {
  const workspace = await chooseWorkspace();
  if (!workspace) { app.quit(); return; }
  const root=path.resolve(__dirname,'..');
  const runtime=path.join(root,'runtime','bin','agentengine');
  const port=process.env.STUDIO_APP_PORT||'8172';
  const env={...process.env, STUDIO_APP_WORKSPACE:workspace};
  service=spawn(runtime,['studio',workspace,'--port',port],{stdio:'ignore',env});
  await waitForStudio(`http://127.0.0.1:${port}/`);
  const win=new BrowserWindow({width:1440,height:900,show:true, title:'AgentKit Studio'});
  await win.loadURL(`http://127.0.0.1:${port}/`);
}
app.whenReady().then(createWindow).catch(error=>{ console.error(error); dialog.showErrorBox('AgentKit Studio 启动失败',String(error)); app.quit(); });
app.on('before-quit',()=>{if(service) service.kill('SIGTERM');});
