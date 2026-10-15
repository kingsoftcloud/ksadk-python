// Replay actual network-produced presentation frames with observable pause points.
import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';
const frames = JSON.parse(readFileSync(process.env.R85_CONVERSATION_ITEMS, 'utf8'));
const tool = frames.findIndex(f => f.conversationItem.kind === 'tool_call' && f.conversationItem.nativeRef.parentScopeId && f.conversationItem.payload.executionStatus === 'running' && f.conversationItem.payload.tool);
const child = frames.findIndex(f => f.conversationItem.kind === 'agent' && f.conversationItem.payload.status === 'completed');
if (tool < 0 || child <= tool) throw Error('Real network frames lack required tool/child pause points');
let active;
createServer((req,res) => {
  res.setHeader('Access-Control-Allow-Origin','*');
  res.setHeader('Access-Control-Allow-Headers','*');
  if (req.method === 'OPTIONS') {res.end();return;}
  if (req.url === '/stream') {
    if (active) {res.writeHead(409);res.end();return;}
    res.writeHead(200, {'Content-Type':'text/event-stream','Cache-Control':'no-cache'});
    active = {res,index:0,stage:0};
    res.on("close", () => {if (active?.res === res) active=null;});
    advance();
  } else if (req.url === '/advance') {
    advance();res.end('ok');
  } else {res.end('ready');}
}).listen(Number(process.env.R85_STREAM_PORT || 4196),'127.0.0.1');
function advance() {
  if (!active) return;
  const end = [tool+1,child+1,frames.length][active.stage++];
  for (;active.index<end;active.index++) {
    const frame=frames[active.index];
    active.res.write(`id: ${frame.seq}\ndata: ${JSON.stringify({conversationItem:frame.conversationItem})}\n\n`);
  }
  if (end === frames.length) {active.res.end();active=null;}
}
