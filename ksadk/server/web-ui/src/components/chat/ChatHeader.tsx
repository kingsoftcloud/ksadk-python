import { lazy, Suspense, useState } from 'react';
import {
  ExternalLink,
  FolderOpen,
  MoreHorizontal,
  PanelLeft,
  PanelLeftClose,
  TerminalSquare,
} from 'lucide-react';

import { cn } from '@/lib/utils';

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';

import type { ModelCatalogItem } from './types';

type ThinkingMode = 'auto' | 'enabled' | 'disabled';

const LazyNativeTerminalPanel = lazy(() =>
  import('@/components/native/NativeTerminalPanel').then((module) => ({
    default: module.NativeTerminalPanel,
  })),
);

type ModelSelectorProps = {
  availableModels: ModelCatalogItem[];
  selectedModel: string;
  onSelectModel: (modelId: string) => void;
  selectedModelLabel: string;
  modelCatalogLoaded: boolean;
  modelSource: string;
  thinkingEnabled: boolean;
  thinkingMode: ThinkingMode;
  onSelectThinkingMode: (mode: ThinkingMode) => void;
  compact?: boolean;
};

type ChatHeaderProps = ModelSelectorProps & {
  agentName: string;
  currentSessionId: string | null;
  nativeLauncherMode?: boolean;
  isMobile: boolean;
  sidebarOpen: boolean;
  mobileSidebarOpen: boolean;
  onToggleSidebar: () => void;
  mobileActionsOpen: boolean;
  onMobileActionsOpenChange: (open: boolean) => void;
  workspaceEnabled: boolean;
  onOpenWorkspace: () => void;
  nativeManagementLink?: {
    href: string;
    label: string;
    title: string;
  } | null;
  nativeTerminal?: {
    Enabled: boolean;
    Mode?: string | null;
    Protocol?: string | null;
    Path?: string | null;
  } | null;
};

function ModelSelector({
  availableModels,
  selectedModel,
  onSelectModel,
  selectedModelLabel,
  modelCatalogLoaded,
  modelSource,
  thinkingEnabled,
  thinkingMode,
  onSelectThinkingMode,
  compact = false,
}: ModelSelectorProps) {
  const controlClass = cn(
    'rounded-full border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 outline-none transition-colors focus:ring-1 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300',
    compact ? 'w-full' : 'max-w-[13rem]',
  );
  const modelControl = availableModels.length > 1 ? (
    <select
      value={selectedModel}
      onChange={(event) => onSelectModel(event.target.value)}
      className={controlClass}
    >
      {availableModels.map((model) => (
        <option key={model.id} value={model.id}>
          {model.display_name || model.id}
        </option>
      ))}
    </select>
  ) : selectedModelLabel ? (
    <span
      className={cn(controlClass, 'inline-flex max-w-full items-center', compact ? 'justify-start' : '')}
      title={modelSource || selectedModelLabel}
    >
      <span className="truncate">{selectedModelLabel}</span>
    </span>
  ) : (
    <span className="text-sm text-slate-400">
      {modelCatalogLoaded ? '未配置模型' : 'Loading models...'}
    </span>
  );
  const thinkingControl = thinkingEnabled ? (
    <select
      value={thinkingMode}
      onChange={(event) => onSelectThinkingMode(event.target.value as ThinkingMode)}
      className={cn(controlClass, compact ? '' : 'max-w-[9rem]')}
      title="控制模型 thinking/reasoning 参数"
    >
      <option value="auto">思考自动</option>
      <option value="enabled">开启思考</option>
      <option value="disabled">关闭思考</option>
    </select>
  ) : null;

  return (
    <div className={cn('flex min-w-0 gap-2', compact ? 'w-full flex-col' : 'items-center')}>
      {modelControl}
      {thinkingControl}
    </div>
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
  thinkingEnabled,
  thinkingMode,
  onSelectThinkingMode,
  mobileActionsOpen,
  onMobileActionsOpenChange,
  workspaceEnabled,
  onOpenWorkspace,
  nativeManagementLink,
  nativeTerminal,
  nativeLauncherMode = false,
}: ChatHeaderProps) {
  const [terminalOpen, setTerminalOpen] = useState(false);
  const terminalEnabled = Boolean(nativeTerminal?.Enabled);
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
          'flex flex-shrink-0 items-center justify-between border-b border-slate-200/30 bg-white/95 px-3 py-2 backdrop-blur dark:border-slate-800 dark:bg-slate-900/95 sm:px-4',
          isMobile ? 'pt-[calc(var(--safe-area-top)+0.5rem)]' : 'h-14',
        )}
      >
        <div className="flex min-w-0 items-center gap-2">
          {!nativeLauncherMode ? (
            <button
              type="button"
              onClick={onToggleSidebar}
              className="rounded-lg p-2 text-slate-500 transition-colors hover:bg-slate-100 dark:hover:bg-slate-800"
              aria-label={isMobile ? '打开历史记录' : '切换侧边栏'}
            >
              {sidebarToggleIcon}
            </button>
          ) : null}
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100 sm:text-base">
              {agentName}
            </div>
            <div className="text-[11px] text-slate-400 dark:text-slate-500">
              {nativeLauncherMode ? '原生运行时入口' : '智能体'}
            </div>
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
          <div className="flex min-w-0 items-center gap-2">
            {nativeManagementLink ? (
              <a
                href={nativeManagementLink.href}
                target="_blank"
                rel="noreferrer"
                title={nativeManagementLink.title}
                className="inline-flex flex-shrink-0 items-center gap-1.5 whitespace-nowrap rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm font-medium leading-none text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
              >
                <ExternalLink className="h-4 w-4" />
                <span>{nativeManagementLink.label}</span>
              </a>
            ) : null}
            {workspaceEnabled ? (
              <button
                type="button"
                onClick={onOpenWorkspace}
                className="inline-flex flex-shrink-0 items-center gap-1.5 whitespace-nowrap rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm font-medium leading-none text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
              >
                <FolderOpen className="h-4 w-4" />
                <span>Workspace</span>
              </button>
            ) : null}
            {terminalEnabled ? (
              <button
                type="button"
                onClick={() => setTerminalOpen(true)}
                className="inline-flex flex-shrink-0 items-center gap-1.5 whitespace-nowrap rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm font-medium leading-none text-emerald-800 transition hover:bg-emerald-100 dark:border-emerald-900/70 dark:bg-emerald-950/40 dark:text-emerald-200"
              >
                <TerminalSquare className="h-4 w-4" />
                <span>TUI</span>
              </button>
            ) : null}
            {!nativeLauncherMode ? (
              <ModelSelector
                availableModels={availableModels}
                selectedModel={selectedModel}
                onSelectModel={onSelectModel}
                selectedModelLabel={selectedModelLabel}
                modelCatalogLoaded={modelCatalogLoaded}
                modelSource={modelSource}
                thinkingEnabled={thinkingEnabled}
                thinkingMode={thinkingMode}
                onSelectThinkingMode={onSelectThinkingMode}
              />
            ) : null}
            {!nativeLauncherMode && currentSessionId ? (
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
              <SheetDescription className="sr-only">调整当前会话的模型和 Workspace 操作。</SheetDescription>
            </SheetHeader>
            <div className="mt-6 flex flex-col gap-4">
              {workspaceEnabled ? (
                <button
                  type="button"
                  onClick={() => {
                    onMobileActionsOpenChange(false);
                    onOpenWorkspace();
                  }}
                  className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                >
                  <FolderOpen className="h-4 w-4" />
                  工作区文件
                </button>
              ) : null}
              {terminalEnabled ? (
                <button
                  type="button"
                  onClick={() => {
                    onMobileActionsOpenChange(false);
                    setTerminalOpen(true);
                  }}
                  className="inline-flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-3 text-sm font-medium text-emerald-800 transition hover:bg-emerald-100 dark:border-emerald-900/70 dark:bg-emerald-950/40 dark:text-emerald-200"
                >
                  <TerminalSquare className="h-4 w-4" />
                  原生 TUI
                </button>
              ) : null}
              {nativeManagementLink ? (
                <a
                  href={nativeManagementLink.href}
                  target="_blank"
                  rel="noreferrer"
                  title={nativeManagementLink.title}
                  onClick={() => onMobileActionsOpenChange(false)}
                  className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                >
                  <ExternalLink className="h-4 w-4" />
                  {nativeManagementLink.label}
                </a>
              ) : null}
              {!nativeLauncherMode ? (
                <>
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
                      thinkingEnabled={thinkingEnabled}
                      thinkingMode={thinkingMode}
                      onSelectThinkingMode={onSelectThinkingMode}
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
                </>
              ) : (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm leading-6 text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  当前运行时对话在原生管理台中进行；这里保留管理入口和 Workspace 文件操作。
                </div>
              )}
            </div>
          </SheetContent>
        </Sheet>
      ) : null}
      {nativeTerminal && terminalOpen ? (
        <Suspense fallback={null}>
          <LazyNativeTerminalPanel
            capability={nativeTerminal}
            open={terminalOpen}
            onClose={() => setTerminalOpen(false)}
          />
        </Suspense>
      ) : null}
    </>
  );
}
