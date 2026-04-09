import type { CSSProperties } from 'react';

import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

import type { MessageAttachment, PreviewImageSize } from './types';

function buildPreviewImageStyle(
  isMobile: boolean,
  size: PreviewImageSize | null,
): CSSProperties | undefined {
  if (!size || isMobile) {
    return undefined;
  }

  return {
    width: `min(${size.width}px, calc(100vw - 5rem))`,
    height: `min(${size.height}px, calc(100vh - 9rem))`,
  };
}

function buildPreviewDialogStyle(
  isMobile: boolean,
  size: PreviewImageSize | null,
): CSSProperties | undefined {
  if (!size || isMobile) {
    return undefined;
  }

  return {
    width: `min(${size.width + 32}px, calc(100vw - 3rem))`,
  };
}

type AttachmentPreviewProps = {
  attachment: MessageAttachment | null;
  isMobile: boolean;
  previewImageSize: PreviewImageSize | null;
  onClose: () => void;
  onImageLoad: (size: PreviewImageSize) => void;
};

export function AttachmentPreview({
  attachment,
  isMobile,
  previewImageSize,
  onClose,
  onImageLoad,
}: AttachmentPreviewProps) {
  const open = Boolean(attachment?.url);

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <DialogContent
        className={cn(
          'gap-0 overflow-hidden border-slate-700 bg-slate-950 p-0 text-slate-100 shadow-2xl [&>button]:right-3 [&>button]:top-3 [&>button]:rounded-md [&>button]:border [&>button]:border-slate-700 [&>button]:bg-slate-900/70 [&>button]:p-1.5 [&>button]:text-slate-200 [&>button]:opacity-100',
          isMobile
            ? 'left-0 top-0 h-[var(--app-height)] max-h-[var(--app-height)] w-screen max-w-none translate-x-0 translate-y-0 rounded-none border-0'
            : 'max-h-[calc(100vh-2rem)] max-w-[calc(100vw-2rem)] rounded-2xl',
        )}
        style={buildPreviewDialogStyle(isMobile, previewImageSize)}
      >
        <div className="flex items-center gap-3 border-b border-slate-800 px-4 py-3 pr-16">
          <DialogTitle className="truncate text-sm font-medium">{attachment?.name || '附件预览'}</DialogTitle>
        </div>
        <div
          className={cn(
            'flex items-center justify-center overflow-auto bg-slate-950',
            isMobile ? 'h-full p-4 pb-[calc(var(--safe-area-bottom)+1rem)]' : 'max-h-[calc(100vh-7rem)] p-4',
          )}
        >
          {attachment ? (
            attachment.type.startsWith('image/') ? (
              <img
                src={attachment.url}
                alt={attachment.name}
                className={cn(
                  'object-contain',
                  isMobile
                    ? 'h-auto max-h-full w-full max-w-full'
                    : 'h-auto w-auto max-h-[calc(100vh-9rem)] max-w-[calc(100vw-5rem)]',
                )}
                style={buildPreviewImageStyle(isMobile, previewImageSize)}
                onLoad={(event) => {
                  const image = event.currentTarget;
                  onImageLoad({
                    width: image.naturalWidth,
                    height: image.naturalHeight,
                  });
                }}
              />
            ) : (
              <iframe
                src={attachment.url}
                title={attachment.name}
                className="h-full min-h-[24rem] w-full rounded-xl border border-slate-800 bg-white"
              />
            )
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
