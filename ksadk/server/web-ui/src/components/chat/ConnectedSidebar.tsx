import { useSessionStore } from '../../stores/session.js';
import { useBootstrapStore } from '../../stores/bootstrap.js';
import { useStreamingStore } from '../../stores/streaming.js';
import { useResponsiveViewport } from '../../hooks/useResponsiveViewport';
import { useSessionLifecycle } from '../../hooks/useSessionLifecycle';
import { ChatSidebar } from './ChatSidebar';
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '../ui/sheet';
import { useUIStore } from '../../stores/ui.js';
import { isHostedChatEnabled } from '../../utils/capabilities.js';
import { resolveSidebarVisibility } from '../../utils/mobile-layout.js';
import type { ApiFacade } from '../../core/api/types.js';
import type { UiCapabilities } from '../../types/capabilities.js';
import { cn } from '@/lib/utils';
import { sessionTitle } from '../../utils/session-helpers.js';

type ConnectedSidebarProps = {
  api: ApiFacade;
  uiCapabilities: UiCapabilities;
  resetCompaction: () => void;
};

export function ConnectedSidebar({ api, uiCapabilities, resetCompaction }: ConnectedSidebarProps) {
  const agentId = useBootstrapStore(s => s.agentId);
  const sessions = useSessionStore(s => s.sessions);
  const currentSessionId = useSessionStore(s => s.currentSessionId);
  const isStreaming = useStreamingStore(s => s.isStreaming);
  const sidebarOpen = useUIStore(s => s.sidebarOpen);
  const mobileSidebarOpen = useUIStore(s => s.mobileSidebarOpen);

  const { isMobile } = useResponsiveViewport();

  const {
    loadSession,
    createNewSession,
    deleteSession,
  } = useSessionLifecycle({
    agentId,
    currentSessionId,
    isStreaming,
    isMobile,
    uiCapabilities,
    api,
    resetCompaction,
  });

  const hostedChatEnabled = isHostedChatEnabled(uiCapabilities);
  const { desktopSidebarVisible } = resolveSidebarVisibility({
    isMobile,
    desktopSidebarOpen: sidebarOpen,
    mobileSidebarOpen,
  });

  if (!hostedChatEnabled) return null;

  if (!isMobile) {
    return (
      <aside
        className={cn(
          'flex-shrink-0 overflow-hidden border-r border-slate-200 transition-[width] duration-300 ease-in-out dark:border-slate-800',
          desktopSidebarVisible ? 'w-[280px]' : 'w-0 border-r-0',
        )}
      >
        <ChatSidebar
          sessions={sessions}
          currentSessionId={currentSessionId}
          isStreaming={isStreaming}
          onCreateNewSession={createNewSession}
          onSelectSession={loadSession}
          onDeleteSession={deleteSession}
          sessionTitle={sessionTitle}
        />
      </aside>
    );
  }

  return (
    <Sheet open={mobileSidebarOpen} onOpenChange={(v) => useUIStore.getState().setMobileSidebarOpen(v)}>
      <SheetContent
        side="left"
        className="w-[88vw] max-w-sm border-slate-200 bg-slate-50 p-0 dark:border-slate-800 dark:bg-slate-950"
      >
        <SheetTitle className="sr-only">历史记录</SheetTitle>
        <SheetDescription className="sr-only">查看和切换历史对话。</SheetDescription>
        <ChatSidebar
          sessions={sessions}
          currentSessionId={currentSessionId}
          isStreaming={isStreaming}
          onCreateNewSession={createNewSession}
          onSelectSession={loadSession}
          onDeleteSession={deleteSession}
          sessionTitle={sessionTitle}
        />
      </SheetContent>
    </Sheet>
  );
}
