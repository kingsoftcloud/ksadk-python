import React, { Suspense, useEffect, useRef } from 'react';
import { useUIStore } from './stores/ui.js';
import { useBootstrapStore } from './stores/bootstrap.js';
import { useModelStore } from './stores/model.js';
import { useStreamingStore } from './stores/streaming.js';
import { useSessionStore } from './stores/session.js';
import { useArtifactStore } from './stores/artifact.js';

import { buildComposerContextIndicator } from './utils/context.js';
import { resolveComposerMaxHeight } from './utils/mobile-layout.js';
import {
  canAccessWorkspaceFiles,
  resolveWorkspacePanelPresentation,
} from './utils/workspace.js';
import { writePersistedSessionId } from './utils/session.js';
import { resolveNativeManagementLinkFromCapability } from './utils/native-platform.js';
import { isHostedChatEnabled } from './utils/capabilities.js';
import { normalizeThinkingMode } from './utils/model-options.js';
import { useResponsiveViewport } from './hooks/useResponsiveViewport';
import { useBootstrap } from './hooks/useBootstrap';
import { useRunAgent } from './hooks/useRunAgent';
import { useFeedback } from './hooks/useFeedback';
import { useSessionLifecycle } from './hooks/useSessionLifecycle';
import { ConnectedSidebar } from './components/chat/ConnectedSidebar';
import { ConnectedMessageList } from './components/chat/ConnectedMessageList';
import { ConnectedComposer } from './components/chat/ConnectedComposer';
import { ChatHeader } from './components/chat/ChatHeader';
import { NativeRuntimeLauncher } from './components/native/NativeRuntimeLauncher';
import { WorkspacePanelContainer } from './components/workspace/WorkspacePanelContainer';
import { ApiFacadeImpl } from './core/api/facade.js';
import type { ApiFacade } from './core/api/types.js';
import type { UiCapabilities } from './types/capabilities.js';
import type { BootstrapWorkspaceFiles } from './types/bootstrap.js';
import type { RuntimeApiFormat } from './types/api.js';
import {
  clampWorkspacePanelWidth,
  DEFAULT_WORKSPACE_PANEL_WIDTH,
  MIN_WORKSPACE_PANEL_WIDTH,
  MAX_WORKSPACE_PANEL_WIDTH,
  MIN_CHAT_PANEL_WIDTH,
  DESKTOP_SIDEBAR_WIDTH,
} from './utils/layout-constants.js';

const LazyArtifactsPanel = React.lazy(() =>
  import('./components/artifacts/ArtifactsPanel.js').then((m) => ({
    default: m.ArtifactsPanel,
  }))
);

export default function App() {
  const api = useRef(new ApiFacadeImpl()).current;
  const agentId = useBootstrapStore(s => s.agentId);
  const currentSessionId = useSessionStore(s => s.currentSessionId);
  const isStreaming = useStreamingStore(s => s.isStreaming);
  const sidebarOpen = useUIStore(s => s.sidebarOpen);
  const mobileSidebarOpen = useUIStore(s => s.mobileSidebarOpen);
  const mobileActionsOpen = useUIStore(s => s.mobileActionsOpen);
  const agentName = useBootstrapStore(s => s.agentName);
  const selectedModel = useModelStore(s => s.selectedModel);
  const availableModels = useModelStore(s => s.availableModels);
  const modelSource = useModelStore(s => s.modelSource);
  const modelCatalogLoaded = useModelStore(s => s.modelCatalogLoaded);
  const thinkingMode = useModelStore(s => s.thinkingMode);
  const agentFramework = useBootstrapStore(s => s.agentFramework);
  const workspaceFiles = useBootstrapStore(s => s.workspaceFiles) as BootstrapWorkspaceFiles | null;
  const accessMode = useBootstrapStore(s => s.accessMode);
  const workspacePanelOpen = useUIStore(s => s.workspacePanelOpen);
  const workspacePanelWidth = useUIStore(s => s.workspacePanelWidth);
  const workspacePanelFullscreen = useUIStore(s => s.workspacePanelFullscreen);
  const apiFormats = useBootstrapStore(s => s.apiFormats) as RuntimeApiFormat[];
  const uiCapabilities = useBootstrapStore(s => s.capabilities) as UiCapabilities;
  const artifactVisible = useArtifactStore(s => s.visible && Boolean(s.content));
  const queuedDraftRef = useRef<Array<{ text: string; attachments: File[] }>>([]);

  const { isMobile, viewportHeight } = useResponsiveViewport();
  const composerMaxHeight = resolveComposerMaxHeight({ isMobile, viewportHeight });

  const {
    fetchSessions,
    loadSession,
    createNewSession,
    deleteSession,
    currentSessionIdRef,
    agentIdRef,
    runSubscriptionAbortRef,
  } = useSessionLifecycle({
    agentId,
    currentSessionId,
    isStreaming,
    isMobile,
    uiCapabilities,
    api,
    resetCompaction: () => {},
  });

  const { submitDraft, stopGeneration } = useRunAgent({
    agentId,
    currentSessionId,
    agentFramework,
    apiFormats,
    selectedModel,
    thinkingMode,
    uiCapabilities,
    isMobile,
    api,
    currentSessionIdRef,
    agentIdRef,
    queuedDraftRef,
  });

  const { submitResponseFeedback, deleteResponseFeedback, respondToApproval } = useFeedback({
    agentId,
    currentSessionId,
    isStreaming,
    api,
    submitDraft,
  });

  useBootstrap({ fetchSessions });

  useEffect(() => {
    agentIdRef.current = agentId;
    currentSessionIdRef.current = currentSessionId;
    writePersistedSessionId(agentId, currentSessionId);
  }, [agentId, currentSessionId]);

  useEffect(() => {
    if (!isMobile) {
      useUIStore.getState().setMobileSidebarOpen(false);
      useUIStore.getState().setMobileActionsOpen(false);
    } else {
      useUIStore.getState().setWorkspacePanelFullscreen(false);
    }
  }, [isMobile]);

  useEffect(() => {
    if (!workspacePanelOpen) {
      useUIStore.getState().setWorkspacePanelFullscreen(false);
    }
  }, [workspacePanelOpen]);

  useEffect(
    () => () => {
      runSubscriptionAbortRef.current?.abort();
    },
    [],
  );

  const hostedChatEnabled = isHostedChatEnabled(uiCapabilities);
  const thinkingEnabled = Boolean(uiCapabilities.Thinking);
  const workspaceEnabled = canAccessWorkspaceFiles({ workspaceFiles, accessMode });
  const workspacePanelPresentation = resolveWorkspacePanelPresentation({ isMobile });
  const nativeManagementLink = resolveNativeManagementLinkFromCapability({
    capability: uiCapabilities.NativeDashboard,
    agentFramework,
    accessMode,
    origin: window.location.origin,
  });
  const nativeLauncherMode = !hostedChatEnabled;
  const nativeRuntimeLabel =
    agentFramework === 'openclaw'
      ? 'OpenClaw'
      : agentFramework === 'hermes'
        ? 'Hermes'
        : '原生运行时';

  const selectedModelMetadata =
    availableModels.find((model) => model.id === selectedModel) || null;
  const selectedModelLabel = selectedModelMetadata?.display_name || selectedModel || '';
  const { desktopSidebarVisible } = (() => {
    const mobileSidebarOpen = useUIStore.getState().mobileSidebarOpen;
    return {
      desktopSidebarVisible: !isMobile && sidebarOpen && hostedChatEnabled,
    };
  })();

  const closeWorkspacePanel = () => {
    useUIStore.getState().setWorkspacePanelFullscreen(false);
    useUIStore.getState().setWorkspacePanelOpen(false);
  };
  const handleWorkspacePanelResizeStart = (event: React.PointerEvent<HTMLDivElement>) => {
    if (workspacePanelFullscreen || isMobile) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = workspacePanelWidth;
    const sidebarWidth =
      hostedChatEnabled && desktopSidebarVisible ? DESKTOP_SIDEBAR_WIDTH : 0;
    const initialCursor = document.body.style.cursor;
    const initialUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const handlePointerMove = (moveEvent: PointerEvent) => {
      const nextWidth = startWidth + startX - moveEvent.clientX;
      useUIStore.getState().setWorkspacePanelWidth(
        clampWorkspacePanelWidth(nextWidth, window.innerWidth, sidebarWidth),
      );
    };
    const handlePointerEnd = () => {
      document.body.style.cursor = initialCursor;
      document.body.style.userSelect = initialUserSelect;
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerEnd);
      window.removeEventListener('pointercancel', handlePointerEnd);
    };
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerEnd);
    window.addEventListener('pointercancel', handlePointerEnd);
  };

  return (
    <div className="flex h-[var(--app-height)] min-h-[var(--app-height)] overflow-hidden bg-white font-sans text-slate-800 dark:bg-slate-900 dark:text-slate-200">
      <ConnectedSidebar
        api={api}
        uiCapabilities={uiCapabilities}
        resetCompaction={() => {}}
        runSubscriptionAbortRef={runSubscriptionAbortRef}
      />

      <main className="relative flex min-w-0 flex-1 flex-col bg-white dark:bg-slate-900">
        <ChatHeader
          agentName={agentName}
          currentSessionId={currentSessionId}
          nativeLauncherMode={nativeLauncherMode}
          isMobile={isMobile}
          sidebarOpen={sidebarOpen}
          mobileSidebarOpen={mobileSidebarOpen}
          onToggleSidebar={() => {
            if (isMobile) {
              useUIStore.getState().toggleMobileSidebar();
            } else {
              useUIStore.getState().toggleSidebar();
            }
          }}
          availableModels={availableModels}
          selectedModel={selectedModel}
          onSelectModel={(modelId) => {
            useModelStore.getState().setSelectedModel(modelId);
            if (isMobile) {
              useUIStore.getState().setMobileActionsOpen(false);
            }
          }}
          selectedModelLabel={selectedModelLabel}
          modelCatalogLoaded={modelCatalogLoaded}
          modelSource={modelSource}
          thinkingEnabled={thinkingEnabled}
          thinkingMode={thinkingMode}
          onSelectThinkingMode={(mode) => useModelStore.getState().setThinkingMode(normalizeThinkingMode(mode))}
          mobileActionsOpen={mobileActionsOpen}
          onMobileActionsOpenChange={(v) => useUIStore.getState().setMobileActionsOpen(v)}
          workspaceEnabled={workspaceEnabled}
          onOpenWorkspace={() => useUIStore.getState().setWorkspacePanelOpen(true)}
          nativeManagementLink={nativeManagementLink}
          nativeTerminal={uiCapabilities.NativeTerminal}
        />

        {nativeLauncherMode ? (
          <NativeRuntimeLauncher
            productLabel={nativeRuntimeLabel}
            nativeManagementLink={nativeManagementLink}
            nativeTerminal={uiCapabilities.NativeTerminal}
            workspaceEnabled={workspaceEnabled}
            onOpenWorkspace={() => useUIStore.getState().setWorkspacePanelOpen(true)}
          />
        ) : (
          <>
            <ConnectedMessageList
              agentName={agentName}
              isMobile={isMobile}
              onDeleteFeedback={deleteResponseFeedback}
              onSubmitFeedback={submitResponseFeedback}
              onRespondToApproval={respondToApproval}
              submitDraft={submitDraft}
              composerMaxHeight={composerMaxHeight}
            />
            <ConnectedComposer
              composerMaxHeight={composerMaxHeight}
              submitDraft={submitDraft}
              stopGeneration={stopGeneration}
              isMobile={isMobile}
            />
          </>
        )}
      </main>

      <WorkspacePanelContainer
        agentId={agentId}
        capability={workspaceFiles!}
        workspacePanelOpen={workspacePanelOpen}
        workspacePanelWidth={workspacePanelWidth}
        workspacePanelFullscreen={workspacePanelFullscreen}
        isMobile={isMobile}
        workspaceEnabled={workspaceEnabled}
        workspaceFiles={workspaceFiles}
        workspacePanelPresentation={workspacePanelPresentation}
        closeWorkspacePanel={closeWorkspacePanel}
        handleWorkspacePanelResizeStart={handleWorkspacePanelResizeStart}
        MIN_WORKSPACE_PANEL_WIDTH={MIN_WORKSPACE_PANEL_WIDTH}
        MAX_WORKSPACE_PANEL_WIDTH={MAX_WORKSPACE_PANEL_WIDTH}
        MIN_CHAT_PANEL_WIDTH={MIN_CHAT_PANEL_WIDTH}
        clampWorkspacePanelWidth={clampWorkspacePanelWidth}
        api={api}
      />

      {artifactVisible && (
        <Suspense fallback={null}>
          <LazyArtifactsPanel />
        </Suspense>
      )}
    </div>
  );
}
