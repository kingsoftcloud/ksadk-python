import { useEffect, useState, type RefObject } from 'react';

import {
  Bot,
  Check,
  Copy,
  Paperclip,
  RefreshCcw,
  ShieldCheck,
  StopCircle,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  XCircle,
} from 'lucide-react';

import { cn } from '@/lib/utils';

import { MessageMarkdown } from '../MessageMarkdown';
import { shouldRenderFeedbackControls } from '../../utils/feedback.js';
import { formatToolPayload } from '../../utils/tool-display.js';
import { copyTextToClipboard } from '../../utils/clipboard.js';

import type { RunActivity } from '../../stores/streaming.js';
import type { ComposerContextIndicator, Message, MessageAttachment } from './types';

type ChatMessageListProps = {
  agentName: string;
  isMobile: boolean;
  isStreaming: boolean;
  activity: RunActivity | null;
  contextIndicator: ComposerContextIndicator;
  messages: Message[];
  onOpenAttachmentPreview: (attachment: MessageAttachment) => void;
  onRespondToApproval: (options: {
    approvalRequestId: string;
    approve: boolean;
    previousResponseId?: string;
  }) => void;
  onSubmitFeedback: (options: {
    message: Message;
    rating: 'up' | 'down';
    comment?: string;
  }) => void;
  onDeleteFeedback: (message: Message) => void;
  onStopGeneration?: () => void;
  onCancelRemote?: () => void;
  scrollRef: RefObject<HTMLDivElement | null>;
};

function formatElapsed(ms: number) {
  const safe = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  if (minutes <= 0) {
    return `${seconds}s`;
  }
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
}

function formatLag(ms: number) {
  if (ms < 1000) return '刚刚';
  if (ms < 60_000) return `${Math.floor(ms / 1000)} 秒前`;
  return `${Math.floor(ms / 60_000)} 分钟前`;
}

function formatCompactTokenCount(value?: number | null) {
  const safe = Number(value);
  if (!Number.isFinite(safe) || safe <= 0) {
    return '0';
  }
  if (safe < 1000) {
    return String(Math.round(safe));
  }
  if (safe < 1_000_000) {
    return `${Math.round(safe / 100) / 10}k`;
  }
  return `${Math.round(safe / 100_000) / 10}m`;
}

function AnimatedTokenCount({ contextIndicator }: { contextIndicator: ComposerContextIndicator }) {
  const usedTokens = contextIndicator?.usedTokens;
  const contextWindowTokens = contextIndicator?.contextWindowTokens;
  const [pulseKey, setPulseKey] = useState(0);

  useEffect(() => {
    if (!usedTokens) {
      return undefined;
    }
    setPulseKey((key) => key + 1);
    return undefined;
  }, [usedTokens]);

  if (!usedTokens || !contextWindowTokens) {
    return null;
  }

  return (
    <span
      key={pulseKey}
      className="token-count-pulse hidden font-mono text-slate-400 dark:text-slate-500 sm:inline"
      title={`估算 token：${Math.round(usedTokens)} / ${Math.round(contextWindowTokens)}`}
    >
      估算 token {formatCompactTokenCount(usedTokens)} / {formatCompactTokenCount(contextWindowTokens)}
    </span>
  );
}

function RunActivityBanner({
  activity,
  contextIndicator,
  onStopGeneration,
  onCancelRemote,
}: {
  activity: RunActivity;
  contextIndicator: ComposerContextIndicator;
  onStopGeneration?: () => void;
  onCancelRemote?: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const isActive = activity.status === 'connecting' || activity.status === 'running' || activity.status === 'waiting';
  const alive = now - activity.lastEventAt < 20_000;

  const icon =
    activity.status === 'failed' ? (
      <StopCircle className="h-3 w-3 text-rose-500" />
    ) : activity.status === 'completed' ? (
      <Check className="h-3 w-3 text-emerald-500" />
    ) : activity.status === 'stopped' ? (
      <ShieldCheck className="h-3 w-3 text-amber-500" />
    ) : (
      <RefreshCcw className="h-3 w-3 animate-spin text-slate-400" />
    );

  return (
    <div className="inline-flex max-w-full items-center gap-2 rounded-full border border-slate-200/70 bg-white/90 px-2.5 py-1 text-[11px] leading-4 text-slate-500 shadow-sm shadow-slate-900/5 backdrop-blur dark:border-slate-700/60 dark:bg-slate-900/90 dark:text-slate-400">
        {icon}
        <span className="max-w-[16rem] truncate text-slate-600 dark:text-slate-300" title={activity.detail || activity.phase}>
          {activity.phase}
        </span>
        <span>
          {activity.source === 'restore' ? '恢复' : '运行'} {formatElapsed(now - activity.startedAt)}
        </span>
        {isActive ? (
          <span className={cn('inline-block h-1.5 w-1.5 flex-shrink-0 rounded-full', alive ? 'bg-emerald-400' : 'bg-slate-300 dark:bg-slate-600')} title={alive ? '连接存活' : '连接超时'} />
        ) : null}
        <span className="text-slate-400 dark:text-slate-500">
          {activity.eventCount} ev
        </span>
        <AnimatedTokenCount contextIndicator={contextIndicator} />
        <span className="hidden text-slate-400 dark:text-slate-500 sm:inline">
          {formatLag(now - activity.lastEventAt)}
        </span>
        {isActive && (onStopGeneration || onCancelRemote) ? (
          <div className="flex flex-shrink-0 gap-1">
            {onStopGeneration ? (
              <button
                type="button"
                onClick={onStopGeneration}
                className="flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 dark:text-slate-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
              >
                <StopCircle className="h-3 w-3" />
                停止
              </button>
            ) : null}
            {onCancelRemote ? (
              <button
                type="button"
                onClick={onCancelRemote}
                className="flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 dark:text-slate-500 dark:hover:bg-rose-950/30 dark:hover:text-rose-300"
              >
                <XCircle className="h-3 w-3" />
                取消
              </button>
            ) : null}
          </div>
        ) : null}
    </div>
  );
}

function EmptyState({ agentName }: { agentName: string }) {
  return (
    <div className="flex flex-col items-center justify-center min-h-[50vh] px-4">
      <div className="mb-6 h-16 w-16 rounded-2xl bg-gradient-to-br from-blue-500 to-indigo-600
        flex items-center justify-center shadow-lg shadow-blue-500/20">
        <Bot className="h-8 w-8 text-white" />
      </div>
      <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
        有什么我可以帮您的吗？
      </h2>
      <p className="mt-2 text-sm text-slate-500">
        我是 {agentName}，由 Ksyun AgentEngine 驱动
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

function ToolPayloadBlock({
  label,
  tone,
  value,
}: {
  label: string;
  tone: 'input' | 'output';
  value: string;
}) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const formatted = formatToolPayload(value);

  useEffect(() => {
    if (copyState === 'idle') {
      return undefined;
    }
    const timer = window.setTimeout(() => setCopyState('idle'), 1600);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const handleCopy = async () => {
    const ok = await copyTextToClipboard(formatted);
    setCopyState(ok ? 'copied' : 'failed');
  };

  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <div
          className={cn(
            'text-xs font-semibold uppercase',
            tone === 'input' ? 'text-blue-500' : 'text-emerald-500',
          )}
        >
          {label}
        </div>
        <button
          type="button"
          onClick={() => { void handleCopy(); }}
          className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] font-medium text-slate-500 transition hover:bg-white hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-900 dark:hover:text-slate-100"
        >
          {copyState === 'copied' ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
          {copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制'}
        </button>
      </div>
      <div className="custom-scrollbar max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words rounded-xl border border-slate-200/30 bg-white/70 p-3 text-slate-600 shadow-sm dark:border-slate-800 dark:bg-slate-950/40 dark:text-slate-300 sm:max-h-[300px]">
        {formatted}
      </div>
    </div>
  );
}

function FeedbackControls({
  isLastMessage,
  isStreaming,
  message,
  onDeleteFeedback,
  onSubmitFeedback,
}: {
  isLastMessage: boolean;
  isStreaming: boolean;
  message: Message;
  onDeleteFeedback: (message: Message) => void;
  onSubmitFeedback: ChatMessageListProps['onSubmitFeedback'];
}) {
  const [commentOpen, setCommentOpen] = useState(false);
  const [comment, setComment] = useState(message.feedback?.comment || '');
  const visible = shouldRenderFeedbackControls(message, isStreaming, isLastMessage);
  const pending = Boolean(message.feedback?.pending);
  const rating = message.feedback?.rating;

  if (!visible) {
    return null;
  }

  const submitDownFeedback = () => {
    onSubmitFeedback({ message, rating: 'down', comment });
    setCommentOpen(false);
  };

  return (
    <div className="mt-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
        <span className="font-medium">本次回复有帮助吗？</span>
        <button
          type="button"
          disabled={pending}
          onClick={() => onSubmitFeedback({ message, rating: 'up', comment: '' })}
          className={cn(
            'inline-flex items-center gap-1 rounded-full border px-2.5 py-1 font-medium transition disabled:cursor-not-allowed disabled:opacity-60',
            rating === 'up'
              ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/70 dark:bg-emerald-950/30 dark:text-emerald-200'
              : 'border-slate-200 bg-white text-slate-600 hover:border-emerald-200 hover:text-emerald-700 dark:border-slate-800 dark:bg-slate-950/40 dark:text-slate-300 dark:hover:border-emerald-900/70 dark:hover:text-emerald-200',
          )}
        >
          <ThumbsUp className="h-3.5 w-3.5" />
          有帮助
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => {
            setComment(message.feedback?.comment || '');
            setCommentOpen((open) => !open);
          }}
          className={cn(
            'inline-flex items-center gap-1 rounded-full border px-2.5 py-1 font-medium transition disabled:cursor-not-allowed disabled:opacity-60',
            rating === 'down'
              ? 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/70 dark:bg-rose-950/30 dark:text-rose-200'
              : 'border-slate-200 bg-white text-slate-600 hover:border-rose-200 hover:text-rose-700 dark:border-slate-800 dark:bg-slate-950/40 dark:text-slate-300 dark:hover:border-rose-900/70 dark:hover:text-rose-200',
          )}
        >
          <ThumbsDown className="h-3.5 w-3.5" />
          需改进
        </button>
        {message.feedback ? (
          <button
            type="button"
            disabled={pending}
            onClick={() => onDeleteFeedback(message)}
            className="inline-flex items-center gap-1 rounded-full border border-transparent px-2 py-1 font-medium text-slate-400 transition hover:border-slate-200 hover:text-slate-700 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:border-slate-800 dark:hover:text-slate-200"
          >
            <Trash2 className="h-3.5 w-3.5" />
            删除反馈
          </button>
        ) : null}
        {pending ? <span className="text-slate-400">提交中…</span> : null}
      </div>
      {commentOpen ? (
        <div className="max-w-xl rounded-2xl border border-rose-100 bg-rose-50/60 p-3 shadow-sm dark:border-rose-900/60 dark:bg-rose-950/20">
          <textarea
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="可以补充哪里不准确、缺失或不符合预期。"
            className="min-h-[84px] w-full resize-y rounded-xl border border-rose-100 bg-white px-3 py-2 text-sm text-slate-700 outline-none transition placeholder:text-slate-400 focus:border-rose-300 focus:ring-2 focus:ring-rose-100 dark:border-rose-900/70 dark:bg-slate-950 dark:text-slate-100 dark:focus:border-rose-700 dark:focus:ring-rose-950"
          />
          <div className="mt-2 flex flex-wrap justify-end gap-2">
            <button
              type="button"
              onClick={() => setCommentOpen(false)}
              className="rounded-xl px-3 py-1.5 text-xs font-semibold text-slate-500 transition hover:bg-white hover:text-slate-800 dark:text-slate-400 dark:hover:bg-slate-950 dark:hover:text-slate-100"
            >
              取消
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={submitDownFeedback}
              className="rounded-xl bg-rose-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-rose-500 disabled:cursor-not-allowed disabled:opacity-60"
            >
              提交点踩
            </button>
          </div>
        </div>
      ) : rating === 'down' && message.feedback?.comment ? (
        <div className="max-w-xl rounded-xl border border-rose-100 bg-rose-50/50 px-3 py-2 text-xs text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/20 dark:text-rose-200">
          反馈：{message.feedback.comment}
        </div>
      ) : null}
      {message.feedback?.error ? (
        <div className="text-xs text-rose-600 dark:text-rose-300">{message.feedback.error}</div>
      ) : null}
    </div>
  );
}

function ChatMessage({
  agentName,
  isMobile,
  isStreaming,
  isLastMessage,
  message,
  onDeleteFeedback,
  onOpenAttachmentPreview,
  onRespondToApproval,
  onSubmitFeedback,
}: {
  agentName: string;
  isMobile: boolean;
  isStreaming: boolean;
  isLastMessage: boolean;
  message: Message;
  onDeleteFeedback: (message: Message) => void;
  onOpenAttachmentPreview: (attachment: MessageAttachment) => void;
  onRespondToApproval: ChatMessageListProps['onRespondToApproval'];
  onSubmitFeedback: ChatMessageListProps['onSubmitFeedback'];
}) {
  if (message.role === 'user') {
    return (
      <div className="mb-3 flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-blue-600 px-3.5 py-2.5 text-[14px] leading-relaxed text-white dark:bg-blue-500">
          {message.attachments?.length ? (
            <MessageAttachments
              attachments={message.attachments}
              isMobile={isMobile}
              onOpenAttachmentPreview={onOpenAttachmentPreview}
            />
          ) : null}
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="mb-3 max-w-none">
      <div className="mb-1.5 flex items-center gap-2 text-xs text-slate-400">
        <Bot className="w-3.5 h-3.5" />
        <span>{agentName}</span>
      </div>

      {message.attachments?.length ? (
        <MessageAttachments
          attachments={message.attachments}
          isMobile={isMobile}
          onOpenAttachmentPreview={onOpenAttachmentPreview}
        />
      ) : null}

      {message.reasoning ? (
        <details className="group/details mb-3 rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2.5 text-sm text-slate-600 transition-all dark:border-slate-700/30 dark:bg-slate-800/20 dark:text-slate-400">
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
                'group/details mb-3 rounded-xl border px-3 py-2.5 text-sm transition-all',
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
                  <ToolPayloadBlock label="入参 (Args)" tone="input" value={tool.args} />
                ) : null}
                {tool.output ? (
                  <ToolPayloadBlock label="输出 (Output)" tone="output" value={tool.output} />
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

      <FeedbackControls
        isLastMessage={isLastMessage}
        isStreaming={isStreaming}
        message={message}
        onDeleteFeedback={onDeleteFeedback}
        onSubmitFeedback={onSubmitFeedback}
      />
    </div>
  );
}

export function ChatMessageList({
  agentName,
  isMobile,
  isStreaming,
  activity,
  contextIndicator,
  messages,
  onDeleteFeedback,
  onOpenAttachmentPreview,
  onRespondToApproval,
  onSubmitFeedback,
  onStopGeneration,
  onCancelRemote,
  scrollRef,
}: ChatMessageListProps) {
  return (
    <div
      ref={scrollRef}
      className={cn(
        'relative min-h-0 flex-1 overflow-y-auto scroll-smooth',
        isMobile ? 'px-3 py-3' : 'px-4 py-5',
      )}
    >
      <div className={cn('mx-auto flex w-full max-w-[64rem] flex-col', activity ? 'pb-10 sm:pb-10' : 'pb-6 sm:pb-8')}>
        {messages.length === 0 ? (
        <EmptyState agentName={agentName} />
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
                onDeleteFeedback={onDeleteFeedback}
                onOpenAttachmentPreview={onOpenAttachmentPreview}
                onRespondToApproval={onRespondToApproval}
                onSubmitFeedback={onSubmitFeedback}
              />
            ),
          )
        )}
      </div>
      {activity ? (
        <div className="sticky bottom-1 z-20 mx-auto flex w-full max-w-[64rem] justify-end">
          <RunActivityBanner
            activity={activity}
            contextIndicator={contextIndicator}
            onStopGeneration={onStopGeneration}
            onCancelRemote={onCancelRemote}
          />
        </div>
      ) : null}
    </div>
  );
}
