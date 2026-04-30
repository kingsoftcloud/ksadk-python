import type { RefObject } from 'react';

import {
  Bot,
  Check,
  Paperclip,
  RefreshCcw,
  ShieldCheck,
  StopCircle,
  User,
  XCircle,
} from 'lucide-react';

import { cn } from '@/lib/utils';

import { MessageMarkdown } from '../MessageMarkdown';
import { formatToolPayload } from '../../utils/tool-display.js';

import type { Message, MessageAttachment } from './types';

type ChatMessageListProps = {
  agentName: string;
  isMobile: boolean;
  isStreaming: boolean;
  messages: Message[];
  onOpenAttachmentPreview: (attachment: MessageAttachment) => void;
  onRespondToApproval: (options: {
    approvalRequestId: string;
    approve: boolean;
    previousResponseId?: string;
  }) => void;
  scrollRef: RefObject<HTMLDivElement | null>;
};

function EmptyState({ agentName, isMobile }: { agentName: string; isMobile: boolean }) {
  return (
    <div className="flex min-h-[45vh] flex-col items-center justify-center px-2 text-center sm:min-h-[50vh] sm:px-4">
      <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-slate-100 dark:bg-slate-800 sm:mb-6 sm:h-16 sm:w-16">
        <Bot className="h-7 w-7 text-slate-600 dark:text-slate-300 sm:h-8 sm:w-8" />
      </div>
      <h2 className={cn('font-semibold', isMobile ? 'text-xl' : 'text-2xl')}>
        有什么我可以帮您的吗？
      </h2>
      <p className="mx-auto mt-2 max-w-md text-sm text-slate-500 dark:text-slate-400">
        我是 {agentName}，一个由{' '}
        <span className="bg-gradient-to-r from-blue-600 to-indigo-500 bg-clip-text font-semibold text-transparent dark:from-blue-400 dark:to-indigo-300">
          Ksyun AgentEngine
        </span>{' '}
        驱动的智能体。您可以在下方输入消息开始对话。
      </p>
    </div>
  );
}

function MessageAttachments({
  attachments,
  isMobile,
  onOpenAttachmentPreview,
}: {
  attachments: MessageAttachment[];
  isMobile: boolean;
  onOpenAttachmentPreview: (attachment: MessageAttachment) => void;
}) {
  return (
    <div className="mb-3 flex flex-wrap gap-3">
      {attachments.map((attachment, attachmentIndex) =>
        attachment.type.startsWith('image/') ? (
          attachment.url ? (
            <button
              key={`${attachment.name}-${attachmentIndex}`}
              type="button"
              onClick={() => onOpenAttachmentPreview(attachment)}
              className={cn(
                'group relative overflow-hidden rounded-xl border border-slate-200 shadow-sm dark:border-slate-700',
                isMobile ? 'w-full max-w-full' : 'max-w-[200px]',
              )}
            >
              <img
                src={attachment.url}
                alt={attachment.name}
                className={cn(
                  'object-cover transition group-hover:scale-[1.02]',
                  isMobile ? 'max-h-[16rem] w-full max-w-full' : 'max-h-[200px] max-w-[200px]',
                )}
              />
            </button>
          ) : (
            <div
              key={`${attachment.name}-${attachmentIndex}`}
              className={cn(
                'flex items-center justify-center rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-400',
                isMobile ? 'h-28 w-full' : 'h-[120px] w-[200px]',
              )}
            >
              {attachment.name}
            </div>
          )
        ) : (
          <div
            key={`${attachment.name}-${attachmentIndex}`}
            className={cn(
              'flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-100 px-3 py-2 shadow-sm dark:border-slate-700 dark:bg-slate-800',
              isMobile ? 'w-full max-w-full' : 'w-max max-w-full',
            )}
          >
            <Paperclip className="h-4 w-4 flex-shrink-0 text-blue-500" />
            {attachment.url ? (
              <a
                href={attachment.url}
                target="_blank"
                rel="noreferrer"
                className="truncate text-sm text-slate-700 hover:underline dark:text-slate-300"
                title={attachment.name}
              >
                {attachment.name}
              </a>
            ) : (
              <span className="truncate text-sm text-slate-700 dark:text-slate-300" title={attachment.name}>
                {attachment.name}
              </span>
            )}
          </div>
        ),
      )}
    </div>
  );
}

function SystemMessage({ message }: { message: Message }) {
  return (
    <div className="w-full px-0 py-2 sm:px-4">
      <div className="mx-auto max-w-3xl rounded-2xl border border-amber-200/80 bg-amber-50/80 px-4 py-3 text-sm text-amber-900 shadow-sm dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
        <div className="flex items-center gap-2 font-medium">
          {message.status === 'running' ? (
            <RefreshCcw className="h-4 w-4 animate-spin text-amber-600 dark:text-amber-300" />
          ) : message.status === 'failed' ? (
            <StopCircle className="h-4 w-4 text-rose-600 dark:text-rose-400" />
          ) : (
            <Check className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
          )}
          <span>{message.content}</span>
        </div>
        {message.compactedUntilSeqId ? (
          <div className="mt-1 text-xs text-amber-700/80 dark:text-amber-200/80">
            已折叠到会话事件 #{message.compactedUntilSeqId}
          </div>
        ) : null}
        {message.summary ? (
          <details className="mt-3 rounded-xl border border-amber-200/80 bg-white/70 px-3 py-2 dark:border-amber-900/60 dark:bg-slate-950/40">
            <summary className="cursor-pointer select-none text-xs font-medium text-amber-800 dark:text-amber-200">
              查看压缩摘要
            </summary>
            <div className="mt-2 text-[13px] leading-relaxed text-slate-700 dark:text-slate-200">
              <MessageMarkdown content={message.summary} />
            </div>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function ChatMessage({
  agentName,
  isMobile,
  isStreaming,
  isLastMessage,
  message,
  onOpenAttachmentPreview,
  onRespondToApproval,
}: {
  agentName: string;
  isMobile: boolean;
  isStreaming: boolean;
  isLastMessage: boolean;
  message: Message;
  onOpenAttachmentPreview: (attachment: MessageAttachment) => void;
  onRespondToApproval: ChatMessageListProps['onRespondToApproval'];
}) {
  return (
    <div className={cn('flex w-full gap-3 py-4 sm:gap-4 sm:px-4', isMobile ? 'px-0' : 'px-4')}>
      <div className="mt-0.5 flex-shrink-0">
        {message.role === 'user' ? (
          <div className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900 sm:h-7 sm:w-7">
            <User className="h-3.5 w-3.5 sm:h-4 sm:w-4" />
          </div>
        ) : (
          <div className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-600 p-1 text-white sm:h-7 sm:w-7">
            <Bot className="h-3.5 w-3.5 sm:h-4 sm:w-4" />
          </div>
        )}
      </div>

      <div className="min-w-0 flex-1 overflow-hidden">
        <div className="mb-1 text-sm font-semibold text-slate-800 dark:text-slate-200">
          {message.role === 'user' ? 'You' : agentName}
        </div>

        {message.attachments?.length ? (
          <MessageAttachments
            attachments={message.attachments}
            isMobile={isMobile}
            onOpenAttachmentPreview={onOpenAttachmentPreview}
          />
        ) : null}

        {message.reasoning ? (
          <details className="group/details mb-4 rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-3 text-sm text-slate-600 transition-all dark:border-slate-700/50 dark:bg-slate-800/20 dark:text-slate-400">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-medium">
              <div className="flex items-center gap-2">
                {isStreaming && isLastMessage && !message.content ? (
                  <RefreshCcw className="h-4 w-4 animate-spin text-emerald-500" />
                ) : (
                  <Check className="h-4 w-4 text-emerald-500" />
                )}
                <span>思考过程</span>
              </div>
            </summary>
            <div className="mx-1 mt-3 border-l-2 border-slate-200 py-1 pl-4 text-[14px] leading-relaxed opacity-90 dark:border-slate-700">
              <MessageMarkdown content={message.reasoning} />
            </div>
          </details>
        ) : null}

        {message.tools
          ? Object.values(message.tools).map((tool, toolIndex) => (
              <details
                key={`${tool.name}-${toolIndex}`}
                open={tool.status === 'paused' ? true : undefined}
                className={cn(
                  'group/details mb-4 rounded-xl border px-4 py-3 text-sm transition-all',
                  tool.status === 'paused'
                    ? 'border-amber-200 bg-amber-50/50 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200'
                    : 'border-blue-200 bg-blue-50/30 text-blue-600 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-400',
                )}
              >
                <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-medium">
                  <div className="flex items-center gap-2">
                    {tool.status === 'running' ? (
                      <RefreshCcw className="h-4 w-4 animate-spin text-blue-500" />
                    ) : tool.status === 'paused' ? (
                      <ShieldCheck className="h-4 w-4 text-amber-500" />
                    ) : (
                      <Check className="h-4 w-4 text-emerald-500" />
                    )}
                    <span>{tool.status === 'paused' ? '等待审批：' : '工具调用：'}{tool.name}</span>
                  </div>
                </summary>
                <div
                  className={cn(
                    'mx-1 mt-3 flex flex-col gap-3 border-l-2 py-1 pl-4 font-mono text-[13px] leading-relaxed opacity-90',
                    tool.status === 'paused' ? 'border-amber-200 dark:border-amber-800' : 'border-blue-200 dark:border-blue-800',
                  )}
                >
                  {tool.status === 'paused' && tool.approvalRequestId ? (
                    <div className="rounded-2xl border border-amber-200 bg-white/75 p-3 font-sans text-sm text-amber-900 shadow-sm dark:border-amber-900/70 dark:bg-slate-950/40 dark:text-amber-100">
                      <div className="font-medium">该工具调用需要人工确认后继续。</div>
                      {tool.serverLabel ? (
                        <div className="mt-1 text-xs text-amber-700/80 dark:text-amber-200/80">
                          MCP Server: {tool.serverLabel}
                        </div>
                      ) : null}
                      <div className="mt-3 flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={tool.approvalStatus === 'approved' || tool.approvalStatus === 'rejected'}
                          onClick={() =>
                            onRespondToApproval({
                              approvalRequestId: tool.approvalRequestId || '',
                              approve: true,
                              previousResponseId: tool.previousResponseId,
                            })
                          }
                          className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-55"
                        >
                          <Check className="h-3.5 w-3.5" />
                          批准并继续
                        </button>
                        <button
                          type="button"
                          disabled={tool.approvalStatus === 'approved' || tool.approvalStatus === 'rejected'}
                          onClick={() =>
                            onRespondToApproval({
                              approvalRequestId: tool.approvalRequestId || '',
                              approve: false,
                              previousResponseId: tool.previousResponseId,
                            })
                          }
                          className="inline-flex items-center gap-1.5 rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-xs font-semibold text-rose-600 shadow-sm transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-55 dark:border-rose-900/70 dark:bg-slate-950 dark:text-rose-300 dark:hover:bg-rose-950/30"
                        >
                          <XCircle className="h-3.5 w-3.5" />
                          拒绝
                        </button>
                      </div>
                    </div>
                  ) : null}
                  {tool.args ? (
                    <div>
                      <div className="mb-1 text-xs font-semibold uppercase text-blue-500">入参 (Args)</div>
                      <div className="whitespace-pre-wrap break-words text-slate-600 dark:text-slate-300">
                        {formatToolPayload(tool.args)}
                      </div>
                    </div>
                  ) : null}
                  {tool.output ? (
                    <div>
                      <div className="mb-1 text-xs font-semibold uppercase text-emerald-500">
                        输出 (Output)
                      </div>
                      <div className="custom-scrollbar max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words text-slate-600 dark:text-slate-300 sm:max-h-[300px]">
                        {formatToolPayload(tool.output)}
                      </div>
                    </div>
                  ) : null}
                </div>
              </details>
            ))
          : null}

        <div className="w-full break-words">
          {message.content ? (
            <MessageMarkdown content={message.content} />
          ) : isStreaming && isLastMessage && !message.reasoning && !message.tools ? (
            <span className="ml-1 mt-2 inline-block h-4 w-2 animate-pulse rounded-sm bg-emerald-500 align-middle opacity-80 shadow-sm" />
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function ChatMessageList({
  agentName,
  isMobile,
  isStreaming,
  messages,
  onOpenAttachmentPreview,
  onRespondToApproval,
  scrollRef,
}: ChatMessageListProps) {
  return (
    <div
      ref={scrollRef}
      className={cn(
        'min-h-0 flex-1 overflow-y-auto scroll-smooth',
        isMobile ? 'px-3 py-3' : 'px-4 py-5',
      )}
    >
      <div className="mx-auto flex w-full max-w-[64rem] flex-col pb-6 sm:pb-8">
        {messages.length === 0 ? (
          <EmptyState agentName={agentName} isMobile={isMobile} />
        ) : (
          messages.map((message, index) =>
            message.role === 'system' ? (
              <SystemMessage key={message.id || index} message={message} />
            ) : (
              <ChatMessage
                key={message.id || index}
                agentName={agentName}
                isMobile={isMobile}
                isStreaming={isStreaming}
                isLastMessage={index === messages.length - 1}
                message={message}
                onOpenAttachmentPreview={onOpenAttachmentPreview}
                onRespondToApproval={onRespondToApproval}
              />
            ),
          )
        )}
      </div>
    </div>
  );
}
