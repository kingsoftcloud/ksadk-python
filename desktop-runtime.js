const http = require('node:http');
const net = require('node:net');
const {randomBytes} = require('node:crypto');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

async function reservePort(preferred = 0) {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(preferred, '127.0.0.1', resolve);
  });
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

function requestJson(port, route, {method = 'GET', data, cookie, csrf} = {}) {
  return new Promise((resolve, reject) => {
    const body = data === undefined ? undefined : JSON.stringify(data);
    const headers = {};
    if (body) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = Buffer.byteLength(body);
    }
    if (cookie) headers.Cookie = cookie;
    if (csrf) headers['X-CSRF-Token'] = csrf;
    const req = http.request({hostname: '127.0.0.1', port, path: route, method, headers}, res => {
      let raw = '';
      res.on('data', chunk => { raw += chunk; if (raw.length > 4 * 1024 * 1024) req.destroy(new Error('Response too large')); });
      res.on('end', () => {
        try { resolve({status: res.statusCode, headers: res.headers, body: JSON.parse(raw)}); }
        catch { reject(new Error('Invalid JSON from ' + route)); }
      });
    });
    req.setTimeout(2000, () => req.destroy(new Error('Studio request timed out')));
    req.on('error', reject);
    req.end(body);
  });
}

async function waitForOwnedStudio(child, port, token, workspace, timeout = 90000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null || child.spawnError)
      throw new Error('Studio 后端启动失败，请查看应用日志。');
    try {
      const auth = await requestJson(port, '/api/v1/system/session', {method: 'POST', data: {token}});
      if (auth.status === 200) {
        const cookie = (auth.headers['set-cookie'] || []).map(value => value.split(';')[0]).join('; ');
        const bootstrap = await requestJson(port, '/api/v1/system/bootstrap', {cookie});
        if (bootstrap.status === 200 && bootstrap.body.workspace?.path === workspace)
          return {cookie, csrf: auth.body.csrfToken, bootstrap: bootstrap.body};
        throw new Error('Studio 返回的工作区与所选目录不一致。');
      }
    } catch (error) {
      if (error.message.includes('不一致')) throw error;
    }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('无法确认本次启动的 Studio 后端，未连接其他已有服务。');
}

async function startRuntime({resources, workspace, logPath, preferredPort = 0, env = process.env}) {
  workspace = fs.realpathSync(workspace);
  const port = await reservePort(preferredPort);
  const token = randomBytes(32).toString('hex');
  const python = path.join(resources, 'runtime', 'bin', 'python3');
  const childEnv = {
    ...env,
    KSADK_STUDIO_SESSION_TOKEN: token,
    KSADK_STUDIO_LAZY_START: '1',
    // The runtime is inside a signed app bundle. Provider subprocesses must
    // not create __pycache__ files under Contents/Resources after signing.
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONPYCACHEPREFIX: path.join(os.tmpdir(), 'agentkit-studio-pycache'),
  };
  delete childEnv.KSADK_STUDIO_NO_SECURITY;
  delete childEnv.PYTHONPATH;
  delete childEnv.PYTHONHOME;
  const child = spawn(python, ['-I', '-B', '-m', 'ksadk', 'studio', workspace, '--port', String(port), '--no-open'], {
    cwd: workspace, env: childEnv, stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.on('error', error => { child.spawnError = error; });
  fs.mkdirSync(path.dirname(logPath), {recursive: true});
  const output = fs.createWriteStream(logPath, {flags: 'a', mode: 0o600});
  for (const stream of [child.stdout, child.stderr]) {
    let pending = '';
    stream.setEncoding('utf8');
    stream.on('data', chunk => {
      pending += chunk;
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines) output.write(line.split(token).join('[redacted]') + '\n');
    });
    stream.on('end', () => { if (pending) output.write(pending.split(token).join('[redacted]')); });
  }
  child.on('close', () => output.end());
  try {
    const auth = await waitForOwnedStudio(child, port, token, workspace);
    return {child, port, workspace, ...auth, url: 'http://127.0.0.1:' + port + '/#session=' + token};
  } catch (error) { child.kill('SIGTERM'); throw error; }
}

module.exports = {reservePort, requestJson, waitForOwnedStudio, startRuntime};
