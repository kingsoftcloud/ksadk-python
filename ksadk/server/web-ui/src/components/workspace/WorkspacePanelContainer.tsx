import React from 'react';
import { useUIStore } from '../../stores/ui.js';
import { cn } from '@/lib/utils';
import { WorkspacePanel } from './WorkspacePanel';
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '../ui/sheet';
import type { WorkspaceFilesCapability } from '../chat/types';
import type { ApiFacade } from '../../core/api/types.js';

type SheetSide = 'top' | 'bottom' | 'left' | 'right';

type WorkspacePanelContainerProps = {
  agentId: string;
  capability: WorkspaceFilesCapability;
  workspacePanelOpen: boolean;
  workspacePanelWidth: number;
  workspacePanelFullscreen: boolean;
  isMobile: boolean;
  workspaceEnabled: boolean;
  workspaceFiles: WorkspaceFilesCapability | null;
  workspacePanelPresentation: {
    renderMode: string;
    modal?: boolean;
    side?: SheetSide;
    showOverlay?: boolean;
    preventOutsideClose?: boolean;
  };
  closeWorkspacePanel: () => void;
  handleWorkspacePanelResizeStart: (event: React.PointerEvent<HTMLDivElement>) => void;
  api: ApiFacade;
};

export function WorkspacePanelContainer({
  agentId,
  capability,
  workspacePanelOpen,
  workspacePanelWidth,
  workspacePanelFullscreen,
  isMobile,
  workspaceEnabled,
  workspaceFiles,
  workspacePanelPresentation,
  closeWorkspacePanel,
  handleWorkspacePanelResizeStart,
  api,
}: WorkspacePanelContainerProps) {
  const workspacePanelInline = workspacePanelPresentation.renderMode === 'inline';
  const workspacePanelSheet = workspacePanelPresentation.renderMode === 'sheet';

  if (!workspaceEnabled || !workspaceFiles) return null;

  return (
    <>
      {workspacePanelInline ? (
        <>
          {workspacePanelOpen && !workspacePanelFullscreen ? (
            <div
              role="separator"
              aria-label="调整 Workspace 宽度"
              aria-orientation="vertical"
              onPointerDown={handleWorkspacePanelResizeStart}
              className="hidden h-full w-1 cursor-col-resize bg-transparent transition-colors hover:bg-blue-200/60 dark:hover:bg-blue-900/50 md:block"
            />
          ) : null}
          <aside
            style={
              workspacePanelOpen && !workspacePanelFullscreen
                ? { width: `${workspacePanelWidth}px` }
                : undefined
            }
            className={cn(
              workspacePanelFullscreen
                ? 'fixed inset-0 z-40 flex h-[var(--app-height)] w-screen overflow-hidden bg-white dark:bg-slate-950'
                : 'hidden h-full flex-shrink-0 overflow-hidden bg-white transition-[width] duration-200 ease-out dark:bg-slate-950 md:flex',
              workspacePanelOpen
                ? 'border-l border-slate-200/30 dark:border-slate-800/40'
                : 'w-0 border-l border-transparent',
            )}
          >
            {workspacePanelOpen ? (
              <WorkspacePanel
                agentId={agentId}
                capability={capability}
                open={workspacePanelOpen}
                onClose={closeWorkspacePanel}
                isFullscreen={workspacePanelFullscreen}
                onToggleFullscreen={() => useUIStore.getState().setWorkspacePanelFullscreen(!workspacePanelFullscreen)}
                isMobile={isMobile}
                api={api}
              />
            ) : null}
          </aside>
        </>
      ) : null}

      {workspacePanelSheet ? (
        <Sheet
          open={workspacePanelOpen}
          onOpenChange={(open) => {
            if (!open) {
              useUIStore.getState().setWorkspacePanelFullscreen(false);
            }
            useUIStore.getState().setWorkspacePanelOpen(open);
          }}
          modal={workspacePanelPresentation.modal}
        >
          <SheetContent
            side={workspacePanelPresentation.side}
            showOverlay={workspacePanelPresentation.showOverlay}
            onInteractOutside={(event) => {
              if (workspacePanelPresentation.preventOutsideClose) {
                event.preventDefault();
              }
            }}
            showCloseButton={false}
            className={cn(
              'border-slate-200 bg-white p-0 dark:border-slate-800 dark:bg-slate-950',
              isMobile
                ? 'h-[70vh] rounded-t-[1.75rem]'
                : 'h-[calc(100vh-1.5rem)] w-[min(72rem,calc(100vw-1.5rem))] max-w-[calc(100vw-1.5rem)] rounded-l-[1.5rem] border-l shadow-2xl',
            )}
          >
            <SheetTitle className="sr-only">Workspace 文件</SheetTitle>
            <SheetDescription className="sr-only">浏览、上传和预览 Workspace 文件。</SheetDescription>
            <WorkspacePanel
              agentId={agentId}
              capability={capability}
              open={workspacePanelOpen}
              onClose={closeWorkspacePanel}
              isMobile={isMobile}
              api={api}
            />
          </SheetContent>
        </Sheet>
      ) : null}
    </>
  );
}
