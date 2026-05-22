import { useState, useEffect, useRef } from 'react';
import { FileEditor } from './FileEditor.js';
import { MessageMarkdown } from '../MessageMarkdown.js';
import { Eye, Code } from 'lucide-react';
import { cn } from '@/lib/utils';

type MarkdownPreviewProps = {
  content: string;
  path: string;
  onSave: (content: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  getContentRef: React.MutableRefObject<(() => string) | null>;
  isMobile: boolean;
};

function clearTimer(timer: React.MutableRefObject<ReturnType<typeof setTimeout> | null>) {
  if (timer.current) {
    clearTimeout(timer.current);
  }
}

export function MarkdownPreview({
  content,
  path,
  onSave,
  onDirtyChange,
  getContentRef,
  isMobile,
}: MarkdownPreviewProps) {
  const [activeView, setActiveView] = useState<'editor' | 'preview'>('editor');
  const [previewContent, setPreviewContent] = useState(content);

  // Debounce preview updates
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    clearTimer(timerRef);
    timerRef.current = setTimeout(() => setPreviewContent(content), 300);
    return () => clearTimer(timerRef);
  }, [content]);

  if (isMobile) {
    return (
      <div className="flex h-full flex-col">
        <div className="flex items-center gap-1 border-b border-slate-200/30 px-2 py-1.5 dark:border-slate-700/30">
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
          <div className="flex-1 overflow-auto p-4">
            <MessageMarkdown content={previewContent} />
          </div>
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
          className="absolute right-3 top-3 z-10 inline-flex items-center gap-1.5 rounded-lg border border-slate-200/30 bg-white/90 px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm backdrop-blur-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800/90 dark:text-slate-300 dark:hover:bg-slate-700"
        >
          <Eye className="h-3.5 w-3.5" />
          预览
        </button>
      </div>
    );
  }

  // Preview view with back-to-editor button
  return (
    <div className="relative flex h-full flex-col">
      <div className="flex-1 overflow-auto p-4">
        <MessageMarkdown content={previewContent} />
      </div>
      <button
        onClick={() => setActiveView('editor')}
        className="absolute right-3 top-3 z-10 inline-flex items-center gap-1.5 rounded-lg border border-slate-200/30 bg-white/90 px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm backdrop-blur-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800/90 dark:text-slate-300 dark:hover:bg-slate-700"
      >
        <Code className="h-3.5 w-3.5" />
        编辑
      </button>
    </div>
  );
}
