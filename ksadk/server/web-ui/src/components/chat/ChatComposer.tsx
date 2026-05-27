import type {
  ChangeEvent,
  ClipboardEvent,
  DragEvent,
  FormEvent,
  KeyboardEvent,
  RefObject,
} from 'react';

import { Paperclip, Send, ShieldCheck, StopCircle } from 'lucide-react';

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
  queuedDrafts: Array<{ text: string; attachments: File[] }>;
  onAppendAttachments: (files: File[]) => void;
  onInputChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  onRemoveAttachment: (index: number) => void;
  onStopGeneration: () => void;
  onCancelRemote?: () => void;
  onSubmit: (text: string, attachments: File[]) => void;
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
  queuedDrafts,
  onAppendAttachments,
  onInputChange,
  onPaste,
  onRemoveAttachment,
  onStopGeneration,
  onCancelRemote,
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

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isStreaming) {
      if (onCancelRemote) {
        onCancelRemote();
      } else {
        onStopGeneration();
      }
      return;
    }
    const text = input.trim();
    if (!text && attachments.length === 0) return;
    onSubmit(text, attachments);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <div className="relative z-10 flex-shrink-0 bg-white/95 px-3 py-3 backdrop-blur dark:bg-slate-900/95 sm:px-4 sm:py-3">
      <div className="mx-auto w-full max-w-[64rem]">
        {queuedDrafts.length > 0 ? (
          <div className="mb-2 rounded-2xl border border-amber-200/80 bg-amber-50/80 px-3 py-2 text-xs text-amber-900 shadow-sm dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <span className="font-semibold">发送队列 · {queuedDrafts.length}</span>
              <span className="text-amber-700/75 dark:text-amber-200/75">当前回复完成后依次发送</span>
            </div>
            <div className="flex flex-col gap-1">
              {queuedDrafts.slice(0, 3).map((draft, index) => {
                const preview = draft.text.trim() || (draft.attachments.length > 0 ? '仅附件消息' : '空消息');
                return (
                  <div
                    key={`${index}-${preview}-${draft.attachments.length}`}
                    className="flex items-center gap-2 rounded-xl bg-white/70 px-2 py-1.5 dark:bg-slate-950/35"
                  >
                    <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-amber-100 font-mono text-[10px] text-amber-700 dark:bg-amber-900/50 dark:text-amber-100">
                      {index + 1}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-slate-700 dark:text-slate-200">
                      {preview}
                    </span>
                    {draft.attachments.length > 0 ? (
                      <span className="flex-shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] text-amber-700 dark:bg-amber-900/50 dark:text-amber-100">
                        {draft.attachments.length} 附件
                      </span>
                    ) : null}
                  </div>
                );
              })}
              {queuedDrafts.length > 3 ? (
                <div className="px-2 pt-0.5 text-[11px] text-amber-700/80 dark:text-amber-200/80">
                  还有 {queuedDrafts.length - 3} 条等待发送
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="flex items-center gap-3">
          <form
            onSubmit={handleSubmit}
            onDragOver={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onDrop={handleDrop}
            className="relative flex min-w-0 flex-1 flex-col rounded-[1.25rem] border border-slate-200 bg-white p-1.5 shadow-[0_8px_22px_rgba(15,23,42,0.07)] transition-all focus-within:border-slate-300 focus-within:ring-1 focus-within:ring-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:focus-within:border-slate-600 dark:focus-within:ring-slate-600"
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

            <div className="flex w-full items-end gap-2">
              <label
                className="relative flex h-9 w-9 flex-shrink-0 items-center justify-center overflow-hidden rounded-xl text-slate-400 transition hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-200"
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
                <Paperclip className="h-5 w-5" />
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
                  'custom-scrollbar min-h-[38px] w-full resize-none border-0 bg-transparent px-2 py-1.5 text-[15px] leading-6 outline-none placeholder:text-slate-400 dark:text-slate-100 dark:placeholder:text-slate-500',
                  isMobile ? 'text-[16px]' : 'text-[15px]',
                )}
                style={{ maxHeight: `${composerMaxHeight}px`, overflowY: 'auto' }}
              />

              <button
                type="submit"
                disabled={!isStreaming && !input.trim() && attachments.length === 0}
                className={cn(
                  'flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl transition-all',
                  isStreaming
                    ? 'bg-slate-900 text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white'
                    : input.trim() || attachments.length > 0
                    ? 'bg-slate-900 text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white'
                    : 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-600',
                )}
                title={isStreaming ? '停止生成' : '发送消息'}
              >
                {isStreaming ? <StopCircle className="h-4 w-4" /> : <Send className="ml-0.5 h-4 w-4" />}
              </button>
            </div>
          </form>

          {composerContextIndicator ? (
            <div
              className={cn(
                'hidden flex-shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-right text-[11px] leading-5 transition-colors sm:inline-flex',
                composerContextIndicator.phase === 'compressing'
                  ? 'bg-amber-50 text-amber-600 dark:bg-amber-950/30 dark:text-amber-300'
                  : composerContextIndicator.phase === 'warning'
                    ? 'bg-rose-50 text-rose-600 dark:bg-rose-950/30 dark:text-rose-300'
                    : 'bg-slate-50 text-slate-400 dark:bg-slate-800/50 dark:text-slate-500',
              )}
            >
              <ShieldCheck className="h-3 w-3" />
              {composerContextIndicator.label}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
