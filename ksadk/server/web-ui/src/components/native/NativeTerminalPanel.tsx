import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CheckCircle2,
  Plus,
  PlugZap,
  RefreshCw,
  TerminalSquare,
  Trash2,
  X,
} from 'lucide-react';
import type { Terminal as XtermTerminal } from '@xterm/xterm';
import type { FitAddon as XtermFitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

import { cn } from '@/lib/utils';

import {
  buildCreateTerminalSessionPayload,
  buildTerminalAttachUrl,
  normalizeTerminalSessions,
  sanitizeTerminalInputForPty,
  TERMINAL_SESSIONS_ENDPOINT,
} from '@/utils/terminal-session';

type NativeTerminalCapability = {
  Enabled: boolean;
  Mode?: string | null;
  Protocol?: string | null;
  Path?: string | null;
};

type TerminalSession = {
  terminal_session_id: string;
  mode: string;
  status: string;
  cols: number;
  rows: number;
  session_id: string;
  cwd: string;
  created_at: string;
  updated_at: string;
  exit_code: number | null;
};

type NativeTerminalPanelProps = {
  capability: NativeTerminalCapability;
  open: boolean;
  onClose: () => void;
};

type TerminalStatus = 'idle' | 'connecting' | 'connected' | 'closed' | 'error';

const TERMINAL_KEEPALIVE_INTERVAL_MS = 20_000;

function decodeBytes(data: ArrayBuffer | Blob | string) {
  if (typeof data === 'string') {
    return Promise.resolve(data);
  }
  if (data instanceof Blob) {
    return data.text();
  }
  return Promise.resolve(new TextDecoder().decode(data));
}

async function readJson(response: Response) {
  const text = await response.text();
  if (!text) {
    return {};
  }
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

export function NativeTerminalPanel({ capability, open, onClose }: NativeTerminalPanelProps) {
  const [status, setStatus] = useState<TerminalStatus>('idle');
  const [terminalSessions, setTerminalSessions] = useState<TerminalSession[]>([]);
  const [activeTerminalSessionId, setActiveTerminalSessionId] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<XtermTerminal | null>(null);
  const fitAddonRef = useRef<XtermFitAddon | null>(null);
  const terminalSessionsRef = useRef<TerminalSession[]>([]);

  const activeSession = useMemo(
    () => terminalSessions.find((session) => session.terminal_session_id === activeTerminalSessionId) || null,
    [activeTerminalSessionId, terminalSessions],
  );

  const refreshSessions = useCallback(async () => {
    setLoadingSessions(true);
    try {
      const response = await fetch(TERMINAL_SESSIONS_ENDPOINT);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const payload = await readJson(response);
      const normalized = normalizeTerminalSessions(payload) as TerminalSession[];
      setTerminalSessions(normalized);
      setActiveTerminalSessionId((current) => {
        if (current && normalized.some((session) => session.terminal_session_id === current)) {
          return current;
        }
        return normalized[0]?.terminal_session_id || null;
      });
    } catch {
      setStatus('error');
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    terminalSessionsRef.current = terminalSessions;
  }, [terminalSessions]);

  const attachTerminalSession = useCallback(async (session: TerminalSession) => {
    if (!containerRef.current) {
      return () => {};
    }
    setStatus('connecting');

    let disposed = false;
    let terminal: XtermTerminal | null = null;
    let fitAddon: XtermFitAddon | null = null;
    let inputDisposable: { dispose: () => void } | null = null;
    let ws: WebSocket | null = null;
    let resizeHandler: (() => void) | null = null;
    let resizeObserver: ResizeObserver | null = null;
    let keepaliveTimer: number | null = null;

    socketRef.current?.close(1000, 'switch terminal session');
    socketRef.current = null;
    terminalRef.current?.dispose();
    terminalRef.current = null;
    fitAddonRef.current = null;
    containerRef.current.innerHTML = '';

    void Promise.all([import('@xterm/xterm'), import('@xterm/addon-fit')]).then(([xtermModule, fitModule]) => {
      if (disposed || !containerRef.current) {
        return;
      }
      const { Terminal } = xtermModule;
      const { FitAddon } = fitModule;
      terminal = new Terminal({
        cursorBlink: true,
        convertEol: true,
        fontFamily:
          'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
        fontSize: 14,
        lineHeight: 1.2,
        scrollback: 6000,
        allowProposedApi: true,
        theme: {
          background: '#020617',
          foreground: '#e5edf5',
          cursor: '#34d399',
          cursorAccent: '#020617',
          selectionBackground: '#1e40af66',
          black: '#020617',
          red: '#fb7185',
          green: '#34d399',
          yellow: '#fbbf24',
          blue: '#60a5fa',
          magenta: '#c084fc',
          cyan: '#22d3ee',
          white: '#e5edf5',
          brightBlack: '#64748b',
          brightRed: '#fda4af',
          brightGreen: '#86efac',
          brightYellow: '#fde68a',
          brightBlue: '#93c5fd',
          brightMagenta: '#d8b4fe',
          brightCyan: '#67e8f9',
          brightWhite: '#ffffff',
        },
      });
      fitAddon = new FitAddon();
      terminal.loadAddon(fitAddon);
      terminal.open(containerRef.current);
      terminalRef.current = terminal;
      fitAddonRef.current = fitAddon;

      const fit = () => {
        try {
          fitAddon?.fit();
        } catch {
          // Fit can fail briefly while the panel is animating into the DOM.
        }
      };

      const sendTerminalSize = () => {
        if (ws?.readyState !== WebSocket.OPEN || !terminal) {
          return;
        }
        ws.send(
          JSON.stringify({
            type: 'resize',
            cols: terminal.cols || session.cols || 80,
            rows: terminal.rows || session.rows || 24,
          }),
        );
      };

      const fitAndNotify = () => {
        fit();
        sendTerminalSize();
      };

      const scheduleFitAndNotify = () => {
        window.requestAnimationFrame(() => {
          if (disposed) {
            return;
          }
          fitAndNotify();
          window.requestAnimationFrame(() => {
            if (!disposed) {
              fitAndNotify();
            }
          });
        });
      };

      window.setTimeout(scheduleFitAndNotify, 0);

      ws = new WebSocket(
        buildTerminalAttachUrl(capability.Path || '/_ksadk/terminal/ws', session.terminal_session_id),
        capability.Protocol || 'ks-terminal.v1',
      );
      socketRef.current = ws;

      ws.addEventListener('open', () => {
        fit();
        ws?.send(
          JSON.stringify({
            type: 'attach',
            terminal_session_id: session.terminal_session_id,
            cols: terminal?.cols || session.cols || 80,
            rows: terminal?.rows || session.rows || 24,
          }),
        );
        scheduleFitAndNotify();
        setStatus('connected');
        terminal?.focus();
      });
      ws.addEventListener('message', (event) => {
        if (typeof event.data === 'string') {
          try {
            const control = JSON.parse(event.data);
            if (control?.type === 'ready') {
              setStatus('connected');
              return;
            }
            if (control?.type === 'pong') {
              return;
            }
            if (control?.type === 'exit') {
              setStatus('closed');
              return;
            }
            if (control?.type === 'error') {
              setStatus('error');
              terminal?.writeln(`\r\n${control.message || 'Terminal error'}`);
              return;
            }
          } catch {
            // Non-control text is rendered as terminal output for compatibility.
          }
        }
        void decodeBytes(event.data).then((text) => {
          if (!text) {
            return;
          }
          terminal?.write(text);
        });
      });
      ws.addEventListener('close', () => {
        setStatus((current) => (current === 'error' ? current : 'closed'));
        socketRef.current = null;
      });
      ws.addEventListener('error', () => {
        setStatus('error');
      });

      inputDisposable = terminal.onData((data) => {
        const sanitized = sanitizeTerminalInputForPty(data);
        if (sanitized && ws?.readyState === WebSocket.OPEN) {
          ws.send(new TextEncoder().encode(sanitized));
        }
      });

      keepaliveTimer = window.setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, TERMINAL_KEEPALIVE_INTERVAL_MS);

      resizeHandler = () => {
        fitAndNotify();
      };
      window.addEventListener('resize', resizeHandler);
      resizeObserver = new ResizeObserver(() => {
        scheduleFitAndNotify();
      });
      resizeObserver.observe(containerRef.current);
    });

    return () => {
      disposed = true;
      inputDisposable?.dispose();
      if (resizeHandler) {
        window.removeEventListener('resize', resizeHandler);
      }
      if (keepaliveTimer !== null) {
        window.clearInterval(keepaliveTimer);
      }
      resizeObserver?.disconnect();
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close(1000, 'panel closed');
      }
      if (socketRef.current === ws) {
        socketRef.current = null;
      }
      terminal?.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
    };
  }, [capability.Path, capability.Protocol]);

  useEffect(() => {
    if (!open || !capability.Enabled) {
      return;
    }
    void refreshSessions();
  }, [capability.Enabled, open, refreshSessions]);

  useEffect(() => {
    const session =
      terminalSessionsRef.current.find(
        (item) => item.terminal_session_id === activeTerminalSessionId,
      ) || null;
    if (!open || !session) {
      return;
    }
    let cleanup: () => void = () => {};
    let cancelled = false;
    void attachTerminalSession(session).then((dispose) => {
      cleanup = dispose;
      if (cancelled) {
        cleanup();
      }
    });
    return () => {
      cancelled = true;
      cleanup();
    };
  }, [activeTerminalSessionId, attachTerminalSession, open]);

  const createTerminalSession = async () => {
    setStatus('connecting');
    const response = await fetch(TERMINAL_SESSIONS_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildCreateTerminalSessionPayload({ mode: capability.Mode || 'tui' })),
    });
    if (!response.ok) {
      setStatus('error');
      return;
    }
    const payload = await readJson(response);
    const session = payload?.session;
    if (!session?.terminal_session_id) {
      setStatus('error');
      return;
    }
    await refreshSessions();
    setActiveTerminalSessionId(session.terminal_session_id);
  };

  const closeTerminalSession = async (terminalSessionId: string) => {
    const remainingSessions = terminalSessions.filter(
      (session) => session.terminal_session_id !== terminalSessionId,
    );
    setTerminalSessions(remainingSessions);
    if (activeTerminalSessionId === terminalSessionId) {
      setActiveTerminalSessionId(remainingSessions[0]?.terminal_session_id || null);
      if (remainingSessions.length === 0) {
        setStatus('idle');
      }
      socketRef.current?.close(1000, 'terminal session deleted');
      socketRef.current = null;
      terminalRef.current?.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
      if (containerRef.current) {
        containerRef.current.innerHTML = '';
      }
    }
    const response = await fetch(`${TERMINAL_SESSIONS_ENDPOINT}/${terminalSessionId}`, { method: 'DELETE' });
    if (!response.ok && response.status !== 404) {
      setStatus('error');
    }
    await refreshSessions();
  };

  if (!open) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 overflow-hidden border border-slate-800 bg-slate-950 text-slate-100 shadow-2xl shadow-slate-950/40 sm:inset-3 sm:rounded-3xl"
    >
      <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-2xl bg-emerald-500/15 text-emerald-300">
            <TerminalSquare className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">Native TUI</div>
            <div className="flex items-center gap-1.5 text-xs text-slate-400">
              <PlugZap className="h-3 w-3" />
              {status}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refreshSessions()}
            className="rounded-xl p-2 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100"
            aria-label="刷新会话列表"
          >
            <RefreshCw className={cn('h-4 w-4', loadingSessions && 'animate-spin')} />
          </button>
          <button
            type="button"
            onClick={() => void createTerminalSession()}
            className="rounded-xl p-2 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100"
            aria-label="新建终端会话"
          >
            <Plus className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl p-2 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100"
            aria-label="关闭 TUI"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="grid h-[calc(100%-4rem)] min-h-0 grid-cols-[18rem_minmax(0,1fr)] bg-slate-950">
        <aside className="flex min-h-0 flex-col border-r border-slate-800 bg-slate-950">
          <div className="border-b border-slate-800 px-4 py-3 text-xs font-medium uppercase tracking-[0.12em] text-slate-500">
            会话
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {terminalSessions.length === 0 ? (
              <button
                type="button"
                onClick={() => void createTerminalSession()}
                className="flex w-full items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-700 px-3 py-5 text-sm text-slate-400 transition hover:bg-slate-900"
              >
                <Plus className="h-4 w-4" />
                新建终端会话
              </button>
            ) : null}
            {terminalSessions.map((session) => (
              <div
                key={session.terminal_session_id}
                role="button"
                tabIndex={0}
                onClick={() => setActiveTerminalSessionId(session.terminal_session_id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setActiveTerminalSessionId(session.terminal_session_id);
                  }
                }}
                className={cn(
                  'group mb-2 rounded-2xl border px-3 py-3 text-left transition',
                  activeTerminalSessionId === session.terminal_session_id
                    ? 'border-emerald-400/50 bg-emerald-500/10'
                    : 'border-slate-800 bg-slate-900/60 hover:bg-slate-900',
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-100">
                      {session.terminal_session_id}
                    </div>
                    <div className="mt-1 text-[11px] text-slate-400">
                      {session.session_id || '未绑定会话'}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    {session.status === 'running' ? (
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                    ) : null}
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        void closeTerminalSession(session.terminal_session_id);
                      }}
                      className="rounded-lg p-1.5 text-slate-500 opacity-0 transition hover:bg-slate-800 hover:text-rose-300 group-hover:opacity-100"
                      title="关闭会话"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                <div className="mt-3 flex items-center gap-2 text-[11px] text-slate-500">
                  <span className="rounded-full border border-slate-700 px-2 py-0.5">{session.mode}</span>
                  <span className="rounded-full border border-slate-700 px-2 py-0.5">
                    {session.cols}x{session.rows}
                  </span>
                  <span className="rounded-full border border-slate-700 px-2 py-0.5">{session.status}</span>
                </div>
              </div>
            ))}
          </div>
        </aside>

        <section className="min-h-0 bg-slate-950 p-2">
          <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-slate-800 bg-slate-950">
            <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900/80 px-4 py-2 text-xs text-slate-400">
              <div className="flex items-center gap-2">
                <span className="rounded-full border border-slate-700 px-2 py-0.5">
                  {activeSession?.terminal_session_id || 'no session'}
                </span>
                <span>{activeSession?.status || 'idle'}</span>
              </div>
              <div className="text-slate-500">
                {activeSession?.cwd || activeSession?.session_id || 'attach to a terminal session'}
              </div>
            </div>
            <div className="relative min-h-0 flex-1">
              {status === 'connecting' ? (
                <div className="absolute left-5 top-5 z-10 rounded-full bg-slate-900/90 px-3 py-1 text-xs text-slate-400">
                  正在连接原生 TUI...
                </div>
              ) : null}
              <div ref={containerRef} className="h-full w-full overflow-hidden bg-slate-950" />
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
