"""A small recovery document that needs neither Core nor built frontend assets."""

RECOVERY_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Studio 恢复</title><style>
body{font:16px/1.7 system-ui,sans-serif;color:#20262f;background:#f7f8fa;margin:0;padding:8vh 24px}
main{max-width:680px;margin:auto}h1{font-size:28px}button,a{font:inherit}
button{padding:8px 16px;margin:8px 12px 8px 0;cursor:pointer}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;
border:1px solid #d7dce2;padding:20px;border-radius:8px}a{color:#245ab2}
</style></head><body><main><h1>Studio 恢复</h1>
<p>这里可以查看插件启动状态、重试或禁用 Teams。团队数据会保留。</p>
<pre id="status" role="status">正在读取状态…</pre>
<p id="error" role="alert"></p>
<button id="refresh">刷新状态</button><button id="retry">重试插件启动</button>
<button id="disable">禁用 Teams</button>
<button id="repair" hidden>备份并升级兼容历史</button>
<p><a href="/">返回 Studio</a> · <a href="/studio-shell/">打开基础工作区</a></p>
</main><script>
const statusNode = document.getElementById('status');
const errorNode = document.getElementById('error');
async function request(path, init = {}) {
  if (init.method === 'POST') {
    const bootstrap = await fetch('/api/v1/system/bootstrap', {credentials:'same-origin'});
    if (!bootstrap.ok) throw new Error('本地会话不可用，请重新打开恢复页面。');
    const session = await bootstrap.json();
    init.headers = {...init.headers, 'X-CSRF-Token': session.csrfToken};
  }
  const response = await fetch(path, {...init, credentials:'same-origin'});
  const body = await response.json();
  if (!response.ok) {
    const error = body.error || {};
    const stage = error.details?.stage;
    throw new Error([error.message || '请求失败', error.code, stage].filter(Boolean).join(' · '));
  }
  return body;
}
async function refresh() {
  const [host, teams] = await Promise.all([
    request('/api/v1/plugin-ecosystems/dsh/recovery'),
    request('/api/v1/plugins/teams/lifecycle')
  ]);
  statusNode.textContent = JSON.stringify({host, teams}, null, 2);
  document.getElementById('repair').hidden = teams.failure?.code !== 'artifact_migration_required';
}
async function run(action) {
  errorNode.textContent = '';
  document.querySelectorAll('button').forEach(button => button.disabled = true);
  try { await action(); }
  catch (error) { errorNode.textContent = error.message; }
  finally { document.querySelectorAll('button').forEach(button => button.disabled = false); }
}
document.getElementById('refresh').onclick = () => run(refresh);
document.getElementById('retry').onclick = () => run(async () => {
  try { await request('/api/v1/plugin-ecosystems/dsh/core/session', {method:'POST'}); }
  finally { await refresh(); }
});
document.getElementById('disable').onclick = () => run(async () => {
  try { await request('/api/v1/plugins/teams/lifecycle', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:false})
  }); }
  finally { await refresh(); }
});
document.getElementById('repair').onclick = () => run(async () => {
  try { await request('/api/v1/plugins/teams/repair', {method:'POST'}); }
  finally { await refresh(); }
});
run(refresh);
</script></body></html>"""
