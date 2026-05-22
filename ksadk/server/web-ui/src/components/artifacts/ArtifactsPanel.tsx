import { useRef } from 'react';
import { X, Download } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useArtifactStore } from '../../stores/artifact.js';
import { buildSandboxedHtml, useIframeMessageHandler } from '../../utils/sandbox.js';

export function ArtifactsPanel() {
  const visible = useArtifactStore((s) => s.visible);
  const content = useArtifactStore((s) => s.content);
  const artifactType = useArtifactStore((s) => s.type);
  const hide = useArtifactStore((s) => s.hide);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  // Set up iframe message handler at top level (Rules of Hooks).
  // When not visible, iframeRef.current is null so the handler
  // rejects all messages via source validation — safe and correct.
  useIframeMessageHandler(iframeRef);

  if (!visible || !content) return null;

  const sandboxedHtml = buildSandboxedHtml(content);

  const handleDownload = () => {
    const blob = new Blob([content], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `artifact.${artifactType === 'svg' ? 'svg' : 'html'}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <aside
      className={cn(
        'flex h-full flex-shrink-0 flex-col border-l border-slate-200/30 bg-white dark:border-slate-800/40 dark:bg-slate-950',
        'w-[min(42rem,50vw)]',
      )}
    >
      {/* Header */}
      <div className="flex h-14 flex-shrink-0 items-center justify-between border-b border-slate-200/30 px-4 dark:border-slate-800/40">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Artifact
          </span>
          <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            {artifactType}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={handleDownload}
            className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900"
            title="Download"
          >
            <Download className="h-3.5 w-3.5" />
            Download
          </button>
          <button
            type="button"
            onClick={hide}
            className="rounded-lg p-1.5 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-900 dark:hover:text-slate-100"
            aria-label="Close Artifact"
            title="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Iframe body */}
      <div className="min-h-0 flex-1">
        <iframe
          ref={iframeRef}
          srcDoc={sandboxedHtml}
          sandbox="allow-scripts allow-downloads"
          title="Artifact Preview"
          className="h-full w-full border-0 bg-white"
        />
      </div>
    </aside>
  );
}
