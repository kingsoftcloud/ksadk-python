import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function readSource(relativePath: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../${relativePath}`, import.meta.url)),
    'utf-8',
  );
}

describe('Native terminal panel bundle boundaries', () => {
  it('keeps the terminal panel behind lazy imports', () => {
    const chatHeader = readSource('components/chat/ChatHeader.tsx');
    const nativeLauncher = readSource('components/native/NativeRuntimeLauncher.tsx');

    expect(chatHeader).toContain("import('@/components/native/NativeTerminalPanel')");
    expect(nativeLauncher).toContain("import('./NativeTerminalPanel')");
    expect(chatHeader).not.toContain("import { NativeTerminalPanel }");
    expect(nativeLauncher).not.toContain("import { NativeTerminalPanel }");
  });

  it('renders the terminal panel as a fullscreen-only surface', () => {
    const terminalPanel = readSource('components/native/NativeTerminalPanel.tsx');

    expect(terminalPanel).toContain('fixed inset-0');
    expect(terminalPanel).toContain('ResizeObserver');
    expect(terminalPanel).not.toContain('setFullscreen');
    expect(terminalPanel).not.toContain('aria-label={fullscreen');
  });

  it('keeps remote TUI sessions alive and filters terminal color responses', () => {
    const terminalPanel = readSource('components/native/NativeTerminalPanel.tsx');

    expect(terminalPanel).toContain('sanitizeTerminalInputForPty');
    expect(terminalPanel).toContain("type: 'ping'");
    expect(terminalPanel).toContain('TERMINAL_KEEPALIVE_INTERVAL_MS');
  });
});
