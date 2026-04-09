import type {
  ChangeEvent,
  ClipboardEvent,
  DragEvent,
  FormEvent,
  KeyboardEvent,
  RefObject,
} from 'react';

import { Paperclip, Send, StopCircle } from 'lucide-react';

import { cn } from '@/lib/utils';

import type { ComposerContextIndicator } from './types';

type ChatComposerProps = {
  attachments: File[];
  composerContextIndicator: ComposerContextIndicator;
  composerMaxHeight: number;
  fileInputRef: RefObject<HTMLInputElement | null>;
  input: string;
  isMobile: boolean;
  isStreaming: boolean;
  onAppendAttachments: (files: File[]) => void;
  onInputChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  onRemoveAttachment: (index: number) => void;
  onStopGeneration: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
};

export function ChatComposer({
  attachments,
  composerContextIndicator,
  composerMaxHeight,
  fileInputRef,
  input,
  isMobile,
  isStreaming,
  onAppendAttachments,
  onInputChange,
  onPaste,
  onRemoveAttachment,
  onStopGeneration,
  onSubmit,
  textareaRef,
}: ChatComposerProps) {
  const placeholderText = isMobile ? '发送消息...' : '发送消息... (Shift + Enter 换行)';

  const handleDrop = (event: DragEvent<HTMLFormElement>) => {
    event.preventDefault();
    event.stopPropagation();

    if (event.dataTransfer.files && event.dataTransfer.files.length > 0) {
      onAppendAttachments(Array.from(event.dataTransfer.files));
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <div className="relative z-10 flex-shrink-0 border-t border-slate-200/70 bg-white/95 px-3 pb-[calc(var(--safe-area-bottom)+0.75rem)] pt-2 backdrop-blur dark:border-slate-800 dark:bg-slate-900/95 sm:px-4 sm:pb-4">
      <div className="mx-auto w-full max-w-[64rem]">
        {isStreaming ? (
          <div className="mb-2 flex justify-center">
            <button
              type="button"
              onClick={onStopGeneration}
              className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-1.5 text-sm shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:hover:bg-slate-700"
            >
              <StopCircle className="h-4 w-4 text-slate-500" />
              <span>停止生成</span>
            </button>
          </div>
        ) : null}

        <form
          onSubmit={onSubmit}
          onDragOver={(event) => {
            event.preventDefault();
            event.stopPropagation();
          }}
          onDrop={handleDrop}
          className="relative flex w-full flex-col rounded-[1.35rem] border border-slate-200 bg-white p-1.5 shadow-sm transition-all focus-within:border-slate-300 focus-within:ring-1 focus-within:ring-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:focus-within:border-slate-600 dark:focus-within:ring-slate-600"
        >
          {attachments.length > 0 ? (
            <div className="mb-1.5 flex flex-wrap gap-2 px-1.5 pt-1.5">
              {attachments.map((file, index) => (
                <div
                  key={`${file.name}-${file.size}-${index}`}
                  className={cn(
                    'flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-100 px-3 py-1.5 text-xs text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300',
                    isMobile ? 'max-w-full' : '',
                  )}
                >
                  <span className="max-w-[10rem] truncate font-medium sm:max-w-[12rem]">
                    {file.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRemoveAttachment(index)}
                    className="text-slate-400 transition hover:text-red-500"
                    aria-label={`移除附件 ${file.name}`}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          <div className={cn('flex w-full gap-1', isMobile ? 'items-end' : 'items-end')}>
            <label
              className="relative ml-0.5 flex h-10 w-10 flex-shrink-0 items-center justify-center overflow-hidden rounded-xl transition hover:bg-slate-100 dark:hover:bg-slate-800"
              title="上传附件"
            >
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(event) => {
                  if (event.target.files && event.target.files.length > 0) {
                    onAppendAttachments(Array.from(event.target.files));
                    event.target.value = '';
                  }
                }}
              />
              <Paperclip className="h-5 w-5 text-slate-400" />
            </label>

            <textarea
              ref={textareaRef}
              rows={1}
              value={input}
              onChange={onInputChange}
              onKeyDown={handleKeyDown}
              onPaste={onPaste}
              placeholder={placeholderText}
              className={cn(
                'custom-scrollbar min-h-[42px] w-full resize-none border-0 bg-transparent px-2 py-2 text-[15px] leading-6 outline-none placeholder:text-slate-400 dark:text-slate-100 dark:placeholder:text-slate-500',
                isMobile ? 'text-[16px]' : 'text-[15px]',
              )}
              style={{ maxHeight: `${composerMaxHeight}px`, overflowY: 'auto' }}
            />

            <button
              type="submit"
              disabled={(!input.trim() && attachments.length === 0) || isStreaming}
              className={cn(
                'mb-0.5 flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl transition-all',
                (input.trim() || attachments.length > 0) && !isStreaming
                  ? 'bg-slate-900 text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white'
                  : 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-600',
              )}
            >
              <Send className="ml-0.5 h-4 w-4" />
            </button>
          </div>
        </form>

        <div className="mt-1.5 flex flex-wrap items-start justify-between gap-x-3 gap-y-1 px-1">
          <div className="max-w-[24rem] text-[11px] text-slate-400 dark:text-slate-500">
            Agent 可能产生不准确的信息，请独立验证。
          </div>
          {composerContextIndicator ? (
            <div
              className={cn(
                'ml-auto text-right text-[11px] leading-5 transition-colors',
                composerContextIndicator.phase === 'compressing'
                  ? 'text-amber-500 dark:text-amber-300'
                  : composerContextIndicator.phase === 'warning'
                    ? 'text-rose-500 dark:text-rose-300'
                    : 'text-slate-400 dark:text-slate-500',
              )}
            >
              {composerContextIndicator.label}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
