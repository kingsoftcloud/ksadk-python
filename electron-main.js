const {app, BrowserWindow} = require('electron');
const {spawn} = require('child_process');
const http = require('http');
const path = require('path');
let service;
function waitForStudio(url, tries=120) {
  return new Promise((resolve,reject)=>{ const tick=()=>{ const req=http.get(url, r=>{ req.destroy(); resolve(); }); req.on('error',()=>{ if(--tries<=0) reject(new Error('Studio did not start')); else setTimeout(tick,250); }); }; tick(); });
}
async function createWindow() {
  const root=path.resolve(__dirname,'..');
  const runtime=path.join(root,'runtime','bin','agentengine');
  service=spawn(runtime,['studio',process.env.STUDIO_APP_WORKSPACE||'.','--port',process.env.STUDIO_APP_PORT||'8172'],{stdio:'ignore'});
  const port=process.env.STUDIO_APP_PORT||'8172'; await waitForStudio(`http://127.0.0.1:${port}/`);
  const win=new BrowserWindow({width:1440,height:900,show:true, title:'AgentKit Studio'});
  await win.loadURL(`http://127.0.0.1:${port}/`);
}
app.whenReady().then(createWindow).catch(console.error);
app.on('before-quit',()=>{if(service) service.kill('SIGTERM');});
