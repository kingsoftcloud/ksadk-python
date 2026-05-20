import React, { useEffect, useRef, useState } from 'react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { Copy, Check, Eye, X } from 'lucide-react';
import { copyTextToClipboard } from '../../utils/clipboard.js';
import { buildSandboxedHtml, useIframeMessageHandler } from '../../utils/sandbox.js';

interface CodeBlockProps {
  language: string;
  value: string;
}

const PREVIEWABLE_LANGS = new Set(['html', 'svg']);

export const CodeBlock: React.FC<CodeBlockProps> = ({ language, value }) => {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [previewOpen, setPreviewOpen] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  // Set up iframe message handler at top level (Rules of Hooks).
  // When previewOpen is false, iframeRef.current is null so the handler
  // rejects all messages via source validation — safe and correct.
  useIframeMessageHandler(iframeRef);

  useEffect(() => {
    if (copyState === 'idle') {
      return undefined;
    }
    const timer = window.setTimeout(() => setCopyState('idle'), 2000);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const handleCopy = async () => {
    const ok = await copyTextToClipboard(value);
    setCopyState(ok ? 'copied' : 'failed');
  };

  const isPreviewable = PREVIEWABLE_LANGS.has(language.toLowerCase());

  const sandboxedHtml = isPreviewable ? buildSandboxedHtml(value) : '';

  return (
    <div className="my-4 rounded-lg overflow-hidden bg-[#1e1e1e] border border-slate-700/50 shadow-sm">
      <div className="flex items-center justify-between px-4 py-1.5 bg-[#2d2d2d] text-slate-300 text-xs font-mono">
        <span className="uppercase">{language || 'text'}</span>
        <div className="flex items-center gap-3">
          {isPreviewable && (
            <button
              type="button"
              onClick={() => setPreviewOpen(true)}
              className="flex items-center gap-1.5 hover:text-white transition-colors py-1"
            >
              <Eye className="w-3.5 h-3.5" />
              <span>Preview</span>
            </button>
          )}
          <button
            type="button"
            onClick={() => { void handleCopy(); }}
            className="flex items-center gap-1.5 hover:text-white transition-colors py-1"
          >
            {copyState === 'copied' ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
            <span>{copyState === 'copied' ? 'Copied!' : copyState === 'failed' ? 'Copy failed' : 'Copy'}</span>
          </button>
        </div>
      </div>
      <div className="overflow-x-auto text-[13.5px]">
        <SyntaxHighlighter
          language={language}
          style={vscDarkPlus}
          customStyle={{ margin: 0, padding: '1rem', background: 'transparent' }}
          PreTag="div"
        >
          {String(value).replace(/\n$/, '')}
        </SyntaxHighlighter>
      </div>

      {/* Preview Dialog */}
      {previewOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black/80 animate-in fade-in-0"
            onClick={() => setPreviewOpen(false)}
          />
          {/* Dialog content */}
          <div className="relative z-50 flex w-full max-w-4xl flex-col rounded-lg border border-slate-700 bg-white shadow-lg duration-200 animate-in zoom-in-95 dark:bg-slate-900 sm:mx-4">
            {/* Header */}
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800">
              <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                HTML Preview
              </span>
              <button
                type="button"
                onClick={() => setPreviewOpen(false)}
                className="rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
              >
                <X className="h-4 w-4 text-slate-500" />
                <span className="sr-only">Close</span>
              </button>
            </div>
            {/* Iframe */}
            <div className="h-[70vh] min-h-[20rem]">
              <iframe
                ref={iframeRef}
                srcDoc={sandboxedHtml}
                sandbox="allow-scripts allow-downloads"
                title="HTML Preview"
                className="h-full w-full border-0 bg-white"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
};