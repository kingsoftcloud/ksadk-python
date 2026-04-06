import { MoreHorizontal, PanelLeft, PanelLeftClose } from 'lucide-react';

import { cn } from '@/lib/utils';

import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';

import type { ModelCatalogItem } from './types';

type ModelSelectorProps = {
  availableModels: ModelCatalogItem[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  selectedModelLabel: string;
  modelCatalogLoaded: boolean;
  modelSource: string;
  compact?: boolean;
};

type ChatHeaderProps = ModelSelectorProps & {
  agentName: string;
  currentSessionId: string | null;
  isMobile: boolean;
  sidebarOpen: boolean;
  mobileSidebarOpen: boolean;
  onToggleSidebar: () => void;
  mobileActionsOpen: boolean;
  onMobileActionsOpenChange: (open: boolean) => void;
};

function ModelSelector({
  availableModels,
  selectedModel,
  onSelectModel,
  selectedModelLabel,
  modelCatalogLoaded,
  modelSource,
  compact = false,
}: ModelSelectorProps) {
  if (availableModels.length > 1) {
    return (
      <select
        value={selectedModel}
        onChange={(event) => onSelectModel(event.target.value)}
        className={cn(
          'rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2 text-sm text-slate-700 outline-none transition-colors focus:ring-1 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300',
          compact ? 'w-full' : 'max-w-[12rem]',
        )}
      >
        {availableModels.map((model) => (
          <option key={model.id} value={model.id}>
            {model.display_name || model.id}
          </option>
        ))}
      </select>
    );
  }

  if (selectedModelLabel) {
    return (
      <span
        className={cn(
          'inline-flex max-w-full items-center rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300',
          compact ? 'w-full justify-start' : 'max-w-[12rem]',
        )}
        title={modelSource || selectedModelLabel}
      >
        <span className="truncate">{selectedModelLabel}</span>
      </span>
    );
  }

  return (
    <span className="text-sm text-slate-400">
      {modelCatalogLoaded ? '未配置模型' : 'Loading models...'}
    </span>
  );
}

export function ChatHeader({
  agentName,
  currentSessionId,
  isMobile,
  sidebarOpen,
  mobileSidebarOpen,
  onToggleSidebar,
  availableModels,
  selectedModel,
  onSelectModel,
  selectedModelLabel,
  modelCatalogLoaded,
  modelSource,
  mobileActionsOpen,
  onMobileActionsOpenChange,
}: ChatHeaderProps) {
  const sidebarToggleIcon =
    isMobile ? (
      mobileSidebarOpen ? <PanelLeftClose className="h-5 w-5" /> : <PanelLeft className="h-5 w-5" />
    ) : sidebarOpen ? (
      <PanelLeftClose className="h-5 w-5" />
    ) : (
      <PanelLeft className="h-5 w-5" />
    );

  return (
    <>
      <header
        className={cn(
          'flex flex-shrink-0 items-center justify-between border-b border-slate-200/70 bg-white/95 px-3 py-2 backdrop-blur dark:border-slate-800 dark:bg-slate-900/95 sm:px-4',
          isMobile ? 'pt-[calc(var(--safe-area-top)+0.5rem)]' : 'h-14',
        )}
      >
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={onToggleSidebar}
            className="rounded-lg p-2 text-slate-500 transition-colors hover:bg-slate-100 dark:hover:bg-slate-800"
            aria-label={isMobile ? '打开历史记录' : '切换侧边栏'}
          >
            {sidebarToggleIcon}
          </button>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100 sm:text-base">
              {agentName}
            </div>
            <div className="text-[11px] text-slate-400 dark:text-slate-500">智能体</div>
          </div>
        </div>

        {isMobile ? (
          <button
            type="button"
            onClick={() => onMobileActionsOpenChange(true)}
            className="rounded-lg p-2 text-slate-500 transition-colors hover:bg-slate-100 dark:hover:bg-slate-800"
            aria-label="打开会话操作"
          >
            <MoreHorizontal className="h-5 w-5" />
          </button>
        ) : (
          <div className="flex items-center gap-3">
            <ModelSelector
              availableModels={availableModels}
              selectedModel={selectedModel}
              onSelectModel={onSelectModel}
              selectedModelLabel={selectedModelLabel}
              modelCatalogLoaded={modelCatalogLoaded}
              modelSource={modelSource}
            />
            {currentSessionId ? (
              <span className="rounded bg-slate-50 px-2 py-1 text-xs font-mono text-slate-400 dark:bg-slate-800">
                ID: {currentSessionId.slice(0, 8)}
              </span>
            ) : null}
          </div>
        )}
      </header>

      {isMobile ? (
        <Sheet open={mobileActionsOpen} onOpenChange={onMobileActionsOpenChange}>
          <SheetContent
            side="bottom"
            className="rounded-t-[1.75rem] border-slate-200 bg-white px-4 pb-[calc(var(--safe-area-bottom)+1rem)] pt-6 dark:border-slate-800 dark:bg-slate-900"
          >
            <SheetHeader className="text-left">
              <SheetTitle>会话设置</SheetTitle>
            </SheetHeader>
            <div className="mt-6 flex flex-col gap-4">
              <div className="space-y-2">
                <div className="text-xs font-medium uppercase tracking-[0.12em] text-slate-400 dark:text-slate-500">
                  模型
                </div>
                <ModelSelector
                  availableModels={availableModels}
                  selectedModel={selectedModel}
                  onSelectModel={onSelectModel}
                  selectedModelLabel={selectedModelLabel}
                  modelCatalogLoaded={modelCatalogLoaded}
                  modelSource={modelSource}
                  compact
                />
              </div>
              <div className="space-y-2">
                <div className="text-xs font-medium uppercase tracking-[0.12em] text-slate-400 dark:text-slate-500">
                  当前会话
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  {currentSessionId ? (
                    <span className="break-all font-mono">{currentSessionId}</span>
                  ) : (
                    '新对话尚未创建会话 ID'
                  )}
                </div>
              </div>
            </div>
          </SheetContent>
        </Sheet>
      ) : null}
    </>
  );
}
