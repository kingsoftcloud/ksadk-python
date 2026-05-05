import { useEffect, useRef, useState } from 'react';
import { Maximize2, Minimize2, PlugZap, TerminalSquare, X } from 'lucide-react';
import type { Terminal as XtermTerminal } from '@xterm/xterm';
import type { FitAddon as XtermFitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

import { cn } from '@/lib/utils';

type NativeTerminalCapability = {
  Enabled: boolean;
  Mode?: string | null;
  Protocol?: string | null;
  Path?: string | null;
};

type NativeTerminalPanelProps = {
  capability: NativeTerminalCapability;
  open: boolean;
  onClose: () => void;
};

type TerminalStatus = 'idle' | 'connecting' | 'connected' | 'closed' | 'error';

function buildTerminalWsUrl(path: string) {
  const url = new URL(path || '/_ksadk/terminal/ws', window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

function decodeBytes(data: ArrayBuffer | Blob | string) {
  if (typeof data === 'string') {
    return Promise.resolve(data);
  }
  if (data instanceof Blob) {
    return data.text();
  }
  return Promise.resolve(new TextDecoder().decode(data));
}

export function NativeTerminalPanel({ capability, open, onClose }: NativeTerminalPanelProps) {
  const [status, setStatus] = useState<TerminalStatus>('idle');
  const [fullscreen, setFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<XtermTerminal | null>(null);
  const fitAddonRef = useRef<XtermFitAddon | null>(null);

  useEffect(() => {
    if (!open || !capability.Enabled || !containerRef.current) {
      return undefined;
    }

    setStatus('connecting');
    let disposed = false;
    let terminal: XtermTerminal | null = null;
    let fitAddon: XtermFitAddon | null = null;
    let inputDisposable: { dispose: () => void } | null = null;
    let ws: WebSocket | null = null;
    const resizeController = { current: () => undefined as void };

    void Promise.all([import('@xterm/xterm'), import('@xterm/addon-fit')]).then(
      ([xtermModule, fitModule]) => {
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
          } catch (_error) {
            // Fit can fail briefly while the panel is animating into the DOM.
          }
        };
        window.setTimeout(fit, 0);

        ws = new WebSocket(
          buildTerminalWsUrl(capability.Path || '/_ksadk/terminal/ws'),
          capability.Protocol || 'ks-terminal.v1',
        );
        socketRef.current = ws;

        ws.addEventListener('open', () => {
          fit();
          ws?.send(
            JSON.stringify({
              type: 'start',
              mode: capability.Mode || 'tui',
              argv: [],
              cols: terminal?.cols || 80,
              rows: terminal?.rows || 24,
            }),
          );
          setStatus('connected');
          terminal?.focus();
        });
        ws.addEventListener('message', (event) => {
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
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(new TextEncoder().encode(data));
          }
        });

        resizeController.current = () => {
          if (ws?.readyState !== WebSocket.OPEN) {
            return;
          }
          fit();
          ws.send(
            JSON.stringify({ type: 'resize', cols: terminal?.cols || 80, rows: terminal?.rows || 24 }),
          );
        };
      },
    );
    const handleResize = () => resizeController.current();
    window.addEventListener('resize', handleResize);

    return () => {
      disposed = true;
      window.removeEventListener('resize', handleResize);
      inputDisposable?.dispose();
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
  }, [capability.Enabled, capability.Mode, capability.Path, capability.Protocol, open]);

  useEffect(() => {
    if (!open || !terminalRef.current || !fitAddonRef.current) {
      return;
    }
    const timer = window.setTimeout(() => {
      try {
        fitAddonRef.current?.fit();
        const socket = socketRef.current;
        const terminal = terminalRef.current;
        if (socket?.readyState === WebSocket.OPEN && terminal) {
          socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols || 80, rows: terminal.rows || 24 }));
        }
      } catch (_error) {
        // Ignore transient layout fit failures.
      }
    }, 60);
    return () => window.clearTimeout(timer);
  }, [fullscreen, open]);

  if (!open) {
    return null;
  }

  return (
    <div
      className={cn(
        'fixed z-50 overflow-hidden border border-slate-800 bg-slate-950 text-slate-100 shadow-2xl shadow-slate-950/40',
        fullscreen
          ? 'inset-3 rounded-3xl'
          : 'bottom-4 right-4 h-[min(42rem,calc(100vh-2rem))] w-[min(72rem,calc(100vw-2rem))] rounded-3xl',
      )}
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
            onClick={() => setFullscreen((value) => !value)}
            className="rounded-xl p-2 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100"
            aria-label={fullscreen ? '退出全屏' : '全屏'}
          >
            {fullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
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

      <div className="h-[calc(100%-4rem)] bg-slate-950 p-2">
        {status === 'connecting' ? (
          <div className="absolute left-5 top-20 z-10 rounded-full bg-slate-900/90 px-3 py-1 text-xs text-slate-400">
            正在连接原生 TUI...
          </div>
        ) : null}
        <div ref={containerRef} className="h-full w-full overflow-hidden rounded-2xl bg-slate-950" />
      </div>
    </div>
  );
}
