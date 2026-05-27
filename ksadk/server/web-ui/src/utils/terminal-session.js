export const TERMINAL_SESSIONS_ENDPOINT = '/_ksadk/terminal/sessions';

const OSC_COLOR_RESPONSE_PATTERN = /\x1b\](?:10|11|12);(?:rgb:)?[0-9a-fA-F]{1,4}\/[0-9a-fA-F]{1,4}\/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)/g;
const VISIBLE_OSC_COLOR_RESPONSE_PATTERN = /\]1[012];(?:rgb:)?[0-9a-fA-F]{1,4}\/[0-9a-fA-F]{1,4}\/[0-9a-fA-F]{1,4}/g;

function toNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : fallback;
}

function updatedAtMs(session) {
  const value = Date.parse(String(session?.updated_at || session?.UpdatedAt || ''));
  return Number.isNaN(value) ? 0 : value;
}

export function buildCreateTerminalSessionPayload(options = {}) {
  const payload = {
    mode: String(options.mode || 'tui').trim() || 'tui',
    cols: toNumber(options.cols, 80),
    rows: toNumber(options.rows, 24),
  };
  const sessionId = String(options.sessionId || options.session_id || '').trim();
  if (sessionId) {
    payload.session_id = sessionId;
  }
  const cwd = String(options.cwd || '').trim();
  if (cwd) {
    payload.cwd = cwd;
  }
  if (options.options && typeof options.options === 'object') {
    payload.options = options.options;
  }
  return payload;
}

export function normalizeTerminalSessions(payload) {
  const rawSessions = Array.isArray(payload?.sessions)
    ? payload.sessions
    : Array.isArray(payload?.Sessions)
      ? payload.Sessions
      : [];
  return rawSessions
    .map((session) => ({
      terminal_session_id: String(session?.terminal_session_id || session?.TerminalSessionId || '').trim(),
      mode: String(session?.mode || session?.Mode || 'tui').trim() || 'tui',
      status: String(session?.status || session?.Status || 'closed').trim() || 'closed',
      cols: toNumber(session?.cols ?? session?.Cols, 80),
      rows: toNumber(session?.rows ?? session?.Rows, 24),
      session_id: String(session?.session_id || session?.SessionId || '').trim(),
      cwd: String(session?.cwd || session?.Cwd || '').trim(),
      created_at: session?.created_at || session?.CreatedAt || '',
      updated_at: session?.updated_at || session?.UpdatedAt || '',
      exit_code: session?.exit_code ?? session?.ExitCode ?? null,
    }))
    .filter((session) => session.terminal_session_id)
    .filter((session) => session.status !== 'closed' && session.status !== 'deleted')
    .sort((left, right) => {
      const leftActive = left.status === 'running' ? 1 : 0;
      const rightActive = right.status === 'running' ? 1 : 0;
      if (leftActive !== rightActive) {
        return rightActive - leftActive;
      }
      return updatedAtMs(right) - updatedAtMs(left);
    });
}

export function buildTerminalAttachUrl(path = '/_ksadk/terminal/ws', terminalSessionId, locationLike) {
  const base =
    locationLike ||
    (typeof window !== 'undefined'
      ? window.location
      : { href: 'http://localhost/', protocol: 'http:' });
  const url = new URL(path || '/_ksadk/terminal/ws', base.href || 'http://localhost/');
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const normalizedId = String(terminalSessionId || '').trim();
  if (normalizedId) {
    url.searchParams.set('terminal_session_id', normalizedId);
  }
  return url.toString();
}

export function sanitizeTerminalInputForPty(data) {
  return String(data || '')
    .replace(OSC_COLOR_RESPONSE_PATTERN, '')
    .replace(VISIBLE_OSC_COLOR_RESPONSE_PATTERN, '');
}
