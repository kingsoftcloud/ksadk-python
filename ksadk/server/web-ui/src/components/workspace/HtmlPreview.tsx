import { useEffect, useRef, useState, useCallback, useId } from 'react';
import { buildSandboxedHtml, useIframeMessageHandler } from '../../utils/sandbox.js';
import { FileEditor } from './FileEditor.js';
import { Eye, Code, ExternalLink } from 'lucide-react';
import { cn } from '@/lib/utils';
import { buildWorkspaceFileBaseUrl, buildWorkspaceFileUrl } from '../../utils/workspace.js';

type HtmlPreviewProps = {
  content: string;
  path: string;
  onSave: (content: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  getContentRef: React.MutableRefObject<(() => string) | null>;
  isMobile: boolean;
  agentId: string;
  contentPath: string;
};

export function HtmlPreview({
  content,
  path,
  onSave,
  onDirtyChange,
  getContentRef,
  isMobile,
}: HtmlPreviewProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const channelId = useId();
  const [activeView, setActiveView] = useState<'editor' | 'preview'>('preview');

  const basePath = buildWorkspaceFileBaseUrl(path);
  const fileUrl = buildWorkspaceFileUrl(path);

  const previewHtml = useCallback(() => {
    return buildSandboxedHtml(content, { basePath, channelId });
  }, [content, basePath, channelId]);

  const [previewSrc, setPreviewSrc] = useState(previewHtml());

  // Debounce preview updates from editor changes
  useEffect(() => {
    const timer = setTimeout(() => {
      setPreviewSrc(previewHtml());
    }, 300);
    return () => clearTimeout(timer);
  }, [previewHtml]);

  useIframeMessageHandler(iframeRef, channelId);

  const openPreviewWindow = useCallback(() => {
    const blob = new Blob([previewSrc], { type: 'text/html;charset=utf-8' });
    const blobUrl = URL.createObjectURL(blob);
    const opened = window.open(blobUrl, '_blank', 'noopener,noreferrer');
    window.setTimeout(() => URL.revokeObjectURL(blobUrl), 30_000);
    if (!opened) {
      window.open(fileUrl, '_blank', 'noopener,noreferrer');
    }
  }, [fileUrl, previewSrc]);

  if (isMobile) {
    return (
      <div className="flex h-full flex-col">
        <div className="flex items-center gap-1 border-b border-slate-200 px-2 py-1.5 dark:border-slate-700">
          <button
            onClick={() => setActiveView('editor')}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors',
              activeView === 'editor'
                ? 'bg-slate-100 text-slate-900 dark:bg-slate-700 dark:text-slate-100'
                : 'text-slate-500 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800',
            )}
          >
            <Code className="h-3.5 w-3.5" />
            编辑
          </button>
          <button
            onClick={() => setActiveView('preview')}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors',
              activeView === 'preview'
                ? 'bg-slate-100 text-slate-900 dark:bg-slate-700 dark:text-slate-100'
                : 'text-slate-500 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800',
            )}
          >
            <Eye className="h-3.5 w-3.5" />
            预览
          </button>
        </div>
        {activeView === 'editor' ? (
          <FileEditor
            content={content}
            path={path}
            onSave={onSave}
            onDirtyChange={onDirtyChange}
            getContentRef={getContentRef}
          />
        ) : (
          <iframe
            ref={iframeRef}
            srcDoc={previewSrc}
            sandbox="allow-scripts allow-downloads allow-same-origin"
            title="HTML 预览"
            className="h-full w-full border-0 bg-white"
          />
        )}
      </div>
    );
  }

  // Desktop: full editor with Preview toggle button
  if (activeView === 'editor') {
    return (
      <div className="relative flex h-full flex-col">
        <FileEditor
          content={content}
          path={path}
          onSave={onSave}
          onDirtyChange={onDirtyChange}
          getContentRef={getContentRef}
        />
        <button
          onClick={() => setActiveView('preview')}
          className="absolute right-3 top-3 z-10 inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white/90 px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm backdrop-blur-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800/90 dark:text-slate-300 dark:hover:bg-slate-700"
        >
          <Eye className="h-3.5 w-3.5" />
          预览
        </button>
      </div>
    );
  }

  // Preview view — open in new window goes to the real server URL
  return (
    <div className="relative flex h-full flex-col">
      <iframe
        ref={iframeRef}
        srcDoc={previewSrc}
        sandbox="allow-scripts allow-downloads allow-same-origin"
        title="HTML 预览"
        className="h-full w-full border-0 bg-white"
      />
      <div className="absolute right-3 top-3 z-10 flex gap-1.5">
        <button
          onClick={openPreviewWindow}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white/90 px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm backdrop-blur-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800/90 dark:text-slate-300 dark:hover:bg-slate-700"
          title="在新窗口中打开当前预览"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          新窗口
        </button>
        <button
          onClick={() => setActiveView('editor')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white/90 px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm backdrop-blur-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800/90 dark:text-slate-300 dark:hover:bg-slate-700"
        >
          <Code className="h-3.5 w-3.5" />
          编辑
        </button>
      </div>
    </div>
  );
}
