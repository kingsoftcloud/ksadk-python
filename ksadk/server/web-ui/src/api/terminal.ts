import { CancelledError } from './client.js';

const TERMINAL_BASE = '/_ksadk/terminal';

function rethrowIfNotCancelled(error: unknown): never {
  if (error instanceof DOMException && error.name === 'AbortError') {
    throw new CancelledError();
  }
  throw error;
}

export async function listTerminalSessions(sessionId: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(
      `${TERMINAL_BASE}/v1/sessions?session_id=${encodeURIComponent(sessionId)}`,
    );
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }
  if (!response.ok) {
    throw new Error(`Terminal sessions request failed: ${response.statusText}`);
  }
  return response.json();
}

export async function createTerminalSession(
  sessionId: string,
  cols: number,
  rows: number,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${TERMINAL_BASE}/v1/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, cols, rows }),
    });
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }
  if (!response.ok) {
    throw new Error(`Create terminal session failed: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteTerminalSession(terminalSessionId: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(
      `${TERMINAL_BASE}/v1/sessions/${encodeURIComponent(terminalSessionId)}`,
      { method: 'DELETE' },
    );
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }
  if (!response.ok) {
    throw new Error(`Delete terminal session failed: ${response.statusText}`);
  }
  return response.json();
}

export function buildTerminalAttachUrl(terminalSessionId: string): string {
  const loc = typeof window !== 'undefined' ? window.location : { href: 'http://localhost/', protocol: 'http:' };
  const url = new URL(`${TERMINAL_BASE}/v1/sessions/attach`, loc.href || 'http://localhost/');
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('terminal_session_id', terminalSessionId);
  return url.toString();
}