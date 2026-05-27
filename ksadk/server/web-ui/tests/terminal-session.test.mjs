import test from 'node:test';
import assert from 'node:assert/strict';

async function loadTerminalUtils() {
  return import('../src/utils/terminal-session.js').catch(() => null);
}

test('terminal session utils build runtime control-plane endpoints', async () => {
  const terminalUtils = await loadTerminalUtils();

  assert.ok(terminalUtils, 'expected terminal session helpers to exist');
  assert.equal(terminalUtils.TERMINAL_SESSIONS_ENDPOINT, '/_ksadk/terminal/sessions');
  assert.equal(
    terminalUtils.buildTerminalAttachUrl('/_ksadk/terminal/ws', 'term-1'),
    'ws://localhost/_ksadk/terminal/ws?terminal_session_id=term-1',
  );
});

test('terminal session utils normalize list payloads and prefer active sessions first', async () => {
  const terminalUtils = await loadTerminalUtils();

  assert.ok(terminalUtils, 'expected terminal session helpers to exist');
  assert.deepEqual(
    terminalUtils.normalizeTerminalSessions({
      sessions: [
        { terminal_session_id: 'term-closed', status: 'closed', updated_at: '2026-05-05T10:00:00Z' },
        { terminal_session_id: 'term-running', status: 'running', updated_at: '2026-05-05T09:00:00Z' },
      ],
    }).map((session) => session.terminal_session_id),
    ['term-running'],
  );
});

test('terminal session utils serialize create payloads safely', async () => {
  const terminalUtils = await loadTerminalUtils();

  assert.ok(terminalUtils, 'expected terminal session helpers to exist');
  assert.deepEqual(
    terminalUtils.buildCreateTerminalSessionPayload({ mode: 'tui', cols: 120, rows: 40, sessionId: 'main' }),
    { mode: 'tui', cols: 120, rows: 40, session_id: 'main' },
  );
});

test('terminal session utils strip xterm OSC color responses before sending input to PTY', async () => {
  const terminalUtils = await loadTerminalUtils();

  assert.ok(terminalUtils, 'expected terminal session helpers to exist');
  assert.equal(
    terminalUtils.sanitizeTerminalInputForPty('\x1b]11;rgb:0202/0606/1717\x1b\\'),
    '',
  );
  assert.equal(
    terminalUtils.sanitizeTerminalInputForPty('echo ok\r\x1b]10;rgb:eeee/eeee/eeee\x07'),
    'echo ok\r',
  );
  assert.equal(
    terminalUtils.sanitizeTerminalInputForPty(']11;rgb:0202/0606/1717]12;rgb:eeee/eeee/eeee'),
    '',
  );
});
