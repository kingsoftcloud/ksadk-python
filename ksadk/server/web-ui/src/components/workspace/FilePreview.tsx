import { FileEditor } from './FileEditor.js';
import { MessageMarkdown } from '../MessageMarkdown.js';

type FilePreviewProps = {
  content: string | null;
  objectUrl?: string;
  kind: 'image' | 'pdf' | 'markdown' | 'text' | 'code' | 'unsupported';
  filename: string;
  mimeType: string;
  path?: string;
};

export function FilePreview({ content, objectUrl, kind, filename, path }: FilePreviewProps) {
  if (kind === 'markdown') {
    return <MessageMarkdown content={content || ''} />;
  }

  if (kind === 'image' && objectUrl) {
    return (
      <div className="flex items-center justify-center p-4">
        <img src={objectUrl} alt={filename} className="max-h-[70vh] max-w-full rounded object-contain" />
      </div>
    );
  }

  if (kind === 'pdf' && objectUrl) {
    return (
      <iframe
        src={objectUrl}
        title={filename}
        className="h-full w-full border-0"
      />
    );
  }

  if ((kind === 'text' || kind === 'code') && content !== null) {
    return (
      <FileEditor
        content={content}
        path={path || filename}
        readOnly
        onSave={() => {}}
        onDirtyChange={() => {}}
        getContentRef={{ current: () => content || '' } as React.MutableRefObject<(() => string) | null>}
      />
    );
  }

  return (
    <div className="flex h-full items-center justify-center px-6 text-center">
      <div className="w-full max-w-md rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-5 py-5 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
        该文件类型暂不支持在线预览，请直接下载查看。
      </div>
    </div>
  );
}
