import type { MouseEvent } from 'react';

import { Plus, Trash2 } from 'lucide-react';

import { cn } from '@/lib/utils';

import type { Session } from './types';

type ChatSidebarProps = {
  sessions: Session[];
  currentSessionId: string | null;
  isStreaming: boolean;
  onCreateNewSession: () => void;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string, event: MouseEvent<HTMLButtonElement>) => void;
  formatDate: (ts?: string | number | null) => string;
  sessionTitle: (session: Session) => string;
  className?: string;
};

export function ChatSidebar({
  sessions,
  currentSessionId,
  isStreaming,
  onCreateNewSession,
  onSelectSession,
  onDeleteSession,
  formatDate,
  sessionTitle,
  className,
}: ChatSidebarProps) {
  return (
    <div className={cn('flex h-full min-h-0 flex-col bg-slate-50 dark:bg-slate-950/80', className)}>
      <div className="border-b border-slate-200/80 p-4 dark:border-slate-800">
        <button
          type="button"
          onClick={onCreateNewSession}
          disabled={isStreaming}
          className="flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-sm font-medium transition-colors hover:bg-slate-200/60 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-slate-800"
        >
          <span className="flex items-center gap-2">
            <Plus className="h-4 w-4" />
            <span>新对话</span>
          </span>
          <span className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] text-slate-500 shadow-sm dark:border-slate-700 dark:bg-slate-900">
            ⌘ N
          </span>
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-2 custom-scrollbar">
        <div className="px-2 py-2 text-xs font-semibold text-slate-400 dark:text-slate-500">
          历史记录
        </div>
        <div className="flex flex-col gap-1">
          {sessions.map((session) => (
            <div
              key={session.SessionId}
              role="button"
              tabIndex={0}
              onClick={() => !isStreaming && onSelectSession(session.SessionId)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  if (!isStreaming) {
                    onSelectSession(session.SessionId);
                  }
                }
              }}
              className={cn(
                'group flex items-start justify-between gap-2 rounded-xl px-3 py-2.5 text-sm transition-colors',
                currentSessionId === session.SessionId
                  ? 'bg-slate-200 font-medium text-slate-900 dark:bg-slate-800 dark:text-slate-50'
                  : 'cursor-pointer text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800/60',
              )}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate">{sessionTitle(session)}</div>
                {session.Summary ? (
                  <div className="mt-0.5 line-clamp-2 text-[11px] text-slate-500 dark:text-slate-400">
                    {session.Summary}
                  </div>
                ) : null}
                <div className="mt-1 text-[10px] text-slate-400 dark:text-slate-500">
                  {formatDate(session.UpdatedAt)}
                </div>
              </div>
              <button
                type="button"
                onClick={(event) => onDeleteSession(session.SessionId, event)}
                className="rounded-lg p-1.5 text-slate-400 transition hover:bg-white hover:text-rose-500 md:opacity-0 md:group-hover:opacity-100 dark:hover:bg-slate-900"
                title="Delete chat"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      </div>

      <div className="border-t border-slate-200/80 px-4 py-3 text-center dark:border-slate-800">
        <div className="text-[10px] font-medium tracking-[0.14em] text-slate-400 dark:text-slate-500">
          POWERED BY
        </div>
        <div className="mt-1 bg-gradient-to-r from-blue-600 to-indigo-500 bg-clip-text text-xs font-bold text-transparent dark:from-blue-400 dark:to-indigo-300">
          Ksyun AgentEngine
        </div>
      </div>
    </div>
  );
}
