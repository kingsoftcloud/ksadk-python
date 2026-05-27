import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowUp,
  Download,
  FileCode2,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  Loader2,
  Maximize2,
  Minimize2,
  Package,
  RefreshCw,
  Save,
  Trash2,
  Upload,
  X,
} from 'lucide-react';

import { cn } from '@/lib/utils';
import { FileEditor } from './FileEditor.js';
import { FilePreview } from './FilePreview.js';
import { HtmlPreview } from './HtmlPreview.js';
import { MarkdownPreview } from './MarkdownPreview.js';
import type { WorkspaceEntry, WorkspaceFilesCapability } from '../chat/types.js';
import type { ApiFacade } from '../../core/api/types.js';
import {
  formatWorkspaceDirectoryPathLabel,
  buildWorkspaceFileUrl,
  isWorkspaceRootPath,
  normalizeWorkspacePath,
  resolveWorkspaceEditKind,
  resolveWorkspacePreviewKind,
} from '../../utils/workspace.js';

const DEFAULT_WORKSPACE_CONTENT_PATH = '/agentengine/api/v1/GetWorkspaceFileContent';
const WORKSPACE_AUTO_REFRESH_MS = 4000;

type LoadEntriesOptions = {
  background?: boolean;
};

function parentWorkspacePath(path: string): string {
  const normalized = normalizeWorkspacePath(path);
  if (!normalized || normalized === '.') {
    return '.';
  }
  const segments = normalized.split('/').filter(Boolean);
  if (segments.length <= 1) {
    return '.';
  }
  return segments.slice(0, -1).join('/');
}

function formatModifiedAt(value?: string | null): string {
  if (!value) {
    return '';
  }
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) {
    return value;
  }
  return new Date(parsed).toLocaleString();
}

function formatSize(sizeBytes?: number | null): string {
  const value = Number(sizeBytes);
  if (!Number.isFinite(value) || value <= 0) {
    return '0 B';
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

type WorkspacePanelProps = {
  agentId: string;
  capability: WorkspaceFilesCapability;
  open: boolean;
  onClose?: () => void;
  isFullscreen?: boolean;
  onToggleFullscreen?: () => void;
  isMobile?: boolean;
  api: ApiFacade;
};

type PreviewKind = 'markdown' | 'text' | 'code' | 'html' | 'image' | 'pdf' | 'unsupported';

type PreviewState = {
  path: string;
  kind: PreviewKind;
  status: 'loading' | 'ready' | 'error';
  mimeType?: string;
  content?: string;
  objectUrl?: string;
  error?: string;
};

const SUPPORTED_PREVIEW_GROUPS = [
  { label: 'Markdown', detail: '.md .markdown .mdx' },
  { label: '代码/文本', detail: '.py .js .ts .tsx .json .yaml .log .txt' },
  { label: '图片', detail: 'PNG JPG GIF WebP SVG' },
  { label: 'PDF', detail: '.pdf' },
  { label: '表格文本', detail: 'CSV TSV' },
];

function buildDownloadHref(entryPath: string) {
  return buildWorkspaceFileUrl(entryPath);
}

function PreviewEmptyState({
  label,
  showSupportedTypes = false,
}: {
  label: string;
  showSupportedTypes?: boolean;
}) {
  return (
    <div className="flex h-full min-h-[16rem] flex-col items-center justify-center px-6 text-center">
      <div className="w-full max-w-md rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-5 py-5 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
        <div>{label}</div>
        {showSupportedTypes ? (
          <div className="mt-4 text-left">
            <div className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-400">
              支持在线预览
            </div>
            <div className="mt-2 grid gap-2">
              {SUPPORTED_PREVIEW_GROUPS.map((group) => (
                <div
                  key={group.label}
                  className="rounded-xl border border-slate-200 bg-white px-3 py-2 dark:border-slate-800 dark:bg-slate-950"
                >
                  <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
                    {group.label}
                  </div>
                  <div className="mt-0.5 font-mono text-[11px] text-slate-400">
                    {group.detail}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-3 text-xs leading-5 text-slate-400">
              Word、Excel、PPT 当前不支持在线预览，可下载后查看。
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function WorkspacePanel({
  agentId,
  capability,
  open,
  onClose,
  isFullscreen = false,
  onToggleFullscreen,
  isMobile = false,
  api,
}: WorkspacePanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const previewObjectUrlRef = useRef<string | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [currentPath, setCurrentPath] = useState('.');
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [backgroundRefreshing, setBackgroundRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [previewState, setPreviewState] = useState<PreviewState | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const getContentRef = useRef<(() => string) | null>(null);
  const lastLoadedPreviewPathRef = useRef<string | null>(null);
  const previewLoadReasonRef = useRef<'selection' | 'background-refresh'>('selection');

  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.Path === selectedPath) ?? null,
    [entries, selectedPath],
  );

  const editKind = useMemo(
    () =>
      selectedEntry
        ? resolveWorkspaceEditKind({ path: selectedEntry.Path, mimeType: selectedEntry.MimeType })
        : null,
    [selectedEntry],
  );

  const confirmUnsaved = useCallback((): boolean => {
    if (!dirty) return true;
    return window.confirm('当前文件有未保存的更改，确定要离开吗？');
  }, [dirty]);

  const contentPath = capability.ContentPath || DEFAULT_WORKSPACE_CONTENT_PATH;

  const rootLabel = capability.RootLabel || 'Workspace';
  const displayRootLabel = rootLabel
    ? `${rootLabel.charAt(0).toUpperCase()}${rootLabel.slice(1)}`
    : 'Workspace';
  const currentDirectoryLabel = formatWorkspaceDirectoryPathLabel(currentPath);

  const clearPreviewObjectUrl = useCallback(() => {
    if (previewObjectUrlRef.current) {
      URL.revokeObjectURL(previewObjectUrlRef.current);
      previewObjectUrlRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => clearPreviewObjectUrl();
  }, [clearPreviewObjectUrl]);

  const loadEntries = useCallback(
    async (targetPath: string, options: LoadEntriesOptions = {}) => {
      const background = Boolean(options.background);
      if (background) {
        setBackgroundRefreshing(true);
      } else {
        setLoading(true);
      }
      if (!background) {
        setError('');
      }
      try {
        const data = await api.listWorkspaceFiles(agentId, targetPath, false);
        const workspaceData = data as { Path?: string; Entries?: WorkspaceEntry[] };
        const nextPath = normalizeWorkspacePath(String(workspaceData?.Path || targetPath || '.'));
        const nextEntries = Array.isArray(workspaceData?.Entries) ? (workspaceData.Entries as WorkspaceEntry[]) : [];
        setCurrentPath(nextPath);
        setEntries(nextEntries);
        previewLoadReasonRef.current = background ? 'background-refresh' : 'selection';
        setSelectedPath((previousSelectedPath) => {
          if (previousSelectedPath) {
            const stillExists = nextEntries.find((entry) => entry.Path === previousSelectedPath);
            if (stillExists && stillExists.Type === 'file') {
              return previousSelectedPath;
            }
          }
          return nextEntries.find((entry) => entry.Type === 'file')?.Path ?? null;
        });
      } catch (loadError) {
        console.error('Failed to load workspace entries:', loadError);
        if (!background) {
          setError(loadError instanceof Error ? loadError.message : String(loadError));
        }
      } finally {
        if (background) {
          setBackgroundRefreshing(false);
        } else {
          setLoading(false);
        }
      }
    },
    [agentId, api],
  );

  useEffect(() => {
    if (!open || initialized) {
      return;
    }
    setInitialized(true);
    void loadEntries('.');
  }, [initialized, loadEntries, open]);

  useEffect(() => {
    if (!open || dirty || !initialized) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadEntries(currentPath, { background: true });
    }, WORKSPACE_AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [currentPath, dirty, initialized, loadEntries, open]);

  const loadPreview = useCallback(
    async (entry: WorkspaceEntry) => {
      const initialKind = resolveWorkspacePreviewKind({
        path: entry.Path,
        mimeType: entry.MimeType,
      }) as PreviewKind;
      lastLoadedPreviewPathRef.current = entry.Path;
      if (initialKind === 'unsupported') {
        clearPreviewObjectUrl();
        setPreviewState({
          path: entry.Path,
          kind: initialKind,
          status: 'ready',
          mimeType: entry.MimeType ?? undefined,
        });
        return;
      }

      clearPreviewObjectUrl();
      setPreviewState({
        path: entry.Path,
        kind: initialKind,
        status: 'loading',
        mimeType: entry.MimeType ?? undefined,
      });

      try {
        const result = await api.getWorkspaceFileContent(agentId, entry.Path, { asText: false });
        if (result instanceof Blob) {
          const resolvedMimeType = entry.MimeType || '';
          const resolvedKind = resolveWorkspacePreviewKind({
            path: entry.Path,
            mimeType: resolvedMimeType,
          }) as PreviewKind;

          if (resolvedKind === 'image' || resolvedKind === 'pdf') {
            const objectUrl = URL.createObjectURL(result);
            previewObjectUrlRef.current = objectUrl;
            setPreviewState({
              path: entry.Path,
              kind: resolvedKind,
              status: 'ready',
              mimeType: resolvedMimeType,
              objectUrl,
            });
            return;
          }

          const text = await result.text();
          const textKind = resolveWorkspacePreviewKind({
            path: entry.Path,
            mimeType: resolvedMimeType,
          }) as PreviewKind;
          setPreviewState({
            path: entry.Path,
            kind: textKind,
            status: 'ready',
            mimeType: resolvedMimeType,
            content: text,
          });
        } else {
          const resolvedMimeType = entry.MimeType || '';
          const resolvedKind = resolveWorkspacePreviewKind({
            path: entry.Path,
            mimeType: resolvedMimeType,
          }) as PreviewKind;
          setPreviewState({
            path: entry.Path,
            kind: resolvedKind,
            status: 'ready',
            mimeType: resolvedMimeType,
            content: result,
          });
        }
      } catch (previewError) {
        console.error('Failed to preview workspace file:', previewError);
        setPreviewState({
          path: entry.Path,
          kind: initialKind,
          status: 'error',
          mimeType: entry.MimeType ?? undefined,
          error: previewError instanceof Error ? previewError.message : String(previewError),
        });
      }
    },
    [agentId, api, clearPreviewObjectUrl],
  );

  useEffect(() => {
    if (!selectedEntry || selectedEntry.Type !== 'file') {
      clearPreviewObjectUrl();
      setPreviewState(null);
      setDirty(false);
      lastLoadedPreviewPathRef.current = null;
      return;
    }
    if (
      previewLoadReasonRef.current === 'background-refresh'
      && lastLoadedPreviewPathRef.current === selectedEntry.Path
    ) {
      previewLoadReasonRef.current = 'selection';
      return;
    }
    previewLoadReasonRef.current = 'selection';
    void loadPreview(selectedEntry);
  }, [clearPreviewObjectUrl, loadPreview, selectedEntry]);

  const handleUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) {
      return;
    }
    setUploading(true);
    setError('');
    try {
      for (const file of Array.from(files)) {
        if (file.size > capability.MaxUploadBytes) {
          throw new Error(`文件 ${file.name} 超过上传上限`);
        }
        const remotePath = isWorkspaceRootPath(currentPath) ? file.name : `${currentPath}/${file.name}`;
        const formData = new FormData();
        formData.append('file', file);
        formData.append('AgentId', agentId);
        formData.append('Path', remotePath);
        await api.addWorkspaceFile(formData);
      }
      await loadEntries(currentPath);
    } catch (uploadError) {
      console.error('Failed to upload workspace files:', uploadError);
      setError(uploadError instanceof Error ? uploadError.message : String(uploadError));
    } finally {
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      setUploading(false);
    }
  };

  const handleDelete = async (entry: WorkspaceEntry) => {
    if (!capability.SupportsDelete) {
      return;
    }
    const deletePath = normalizeWorkspacePath(entry.Path);
    if (!window.confirm(`删除 ${deletePath} ?`)) {
      return;
    }
    setError('');
    try {
      await api.deleteWorkspaceFile(agentId, deletePath);
      if (selectedPath && normalizeWorkspacePath(selectedPath) === deletePath) {
        setSelectedPath(null);
        setDirty(false);
      }
      const nextPath =
        entry.Type === 'directory'
          && (normalizeWorkspacePath(currentPath) === deletePath
            || normalizeWorkspacePath(currentPath).startsWith(`${deletePath}/`))
          ? parentWorkspacePath(deletePath)
          : currentPath;
      await loadEntries(nextPath);
    } catch (deleteError) {
      console.error('Failed to delete workspace file:', deleteError);
      setError(deleteError instanceof Error ? deleteError.message : String(deleteError));
    }
  };

  const renderPreview = () => {
    if (!selectedEntry || selectedEntry.Type !== 'file') {
      return <PreviewEmptyState label="从左侧选择文件后在这里查看内容预览。" showSupportedTypes />;
    }
    if (!previewState || previewState.path !== selectedEntry.Path || previewState.status === 'loading') {
      return (
        <div className="flex h-full min-h-[16rem] items-center justify-center">
          <div className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-sm text-slate-500 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在加载预览...
          </div>
        </div>
      );
    }
    if (previewState.status === 'error') {
      return (
        <PreviewEmptyState label={previewState.error || '文件预览加载失败。'} />
      );
    }
    if (previewState.kind === 'markdown') {
      return <MarkdownPreview content={previewState.content || ''} path={selectedEntry.Path} onSave={handleSave} onDirtyChange={setDirty} getContentRef={getContentRef} isMobile={isMobile} />;
    }
    if (previewState.kind === 'image' && previewState.objectUrl) {
      return <FilePreview content={null} objectUrl={previewState.objectUrl} kind="image" filename={selectedEntry.Name} mimeType={previewState.mimeType || ''} />;
    }
    if (previewState.kind === 'pdf' && previewState.objectUrl) {
      return <FilePreview content={null} objectUrl={previewState.objectUrl} kind="pdf" filename={selectedEntry.Name} mimeType={previewState.mimeType || ''} />;
    }
    if (previewState.kind === 'html') {
      return (
        <HtmlPreview
          content={previewState.content || ''}
          path={selectedEntry.Path}
          onSave={handleSave}
          onDirtyChange={setDirty}
          getContentRef={getContentRef}
          isMobile={isMobile}
          agentId={agentId}
          contentPath={contentPath}
        />
      );
    }
    if (previewState.kind === 'text' || previewState.kind === 'code') {
      return (
        <FileEditor
          content={previewState.content || ''}
          path={selectedEntry.Path}
          onSave={handleSave}
          onDirtyChange={setDirty}
          getContentRef={getContentRef}
        />
      );
    }
    return <FilePreview content={null} kind="unsupported" filename={selectedEntry.Name} mimeType={previewState.mimeType || ''} />;
  };

  const handleSave = async (content: string) => {
    if (!selectedEntry) return;
    setSaving(true);
    setError('');
    try {
      const blob = new Blob([content], { type: selectedEntry.MimeType || 'text/plain' });
      const formData = new FormData();
      formData.append('file', blob, selectedEntry.Name);
      formData.append('AgentId', agentId);
      formData.append('Path', selectedEntry.Path);
      await api.addWorkspaceFile(formData);
      setDirty(false);
      // Update previewState content so subsequent previews reflect saved content
      setPreviewState((prev) =>
        prev && prev.path === selectedEntry.Path ? { ...prev, content } : prev,
      );
      await loadEntries(currentPath);
    } catch (saveError) {
      console.error('Failed to save workspace file:', saveError);
      setError(saveError instanceof Error ? saveError.message : String(saveError));
    } finally {
      setSaving(false);
    }
  };

  const previewPaneIsEditor =
    selectedEntry?.Type === 'file'
    && previewState?.path === selectedEntry.Path
    && previewState.status === 'ready'
    && (previewState.kind === 'text' || previewState.kind === 'code' || previewState.kind === 'html' || previewState.kind === 'markdown');

  const previewPaneIsPdf =
    selectedEntry?.Type === 'file'
    && previewState?.path === selectedEntry.Path
    && previewState.status === 'ready'
    && previewState.kind === 'pdf';

  return (
    <div className="flex h-full min-h-[22rem] w-full min-w-0 flex-col bg-white text-slate-800 dark:bg-slate-950 dark:text-slate-200">
      <div className="flex h-14 flex-shrink-0 items-center gap-3 border-b border-slate-200/30 px-4 dark:border-slate-800/40">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
            {displayRootLabel}
          </div>
          <div className="hidden h-4 w-px bg-slate-200/70 dark:bg-slate-800 sm:block" />
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex-shrink-0 text-xs text-slate-400">当前目录</span>
            <div
              className="custom-scrollbar max-w-[min(42vw,34rem)] overflow-x-auto whitespace-nowrap rounded-md bg-slate-50 px-2 py-1 font-mono text-[11px] text-slate-600 dark:bg-slate-900 dark:text-slate-300"
              title={`${currentDirectoryLabel}，来自 ListWorkspaceFiles 返回的 Data.Path 字段`}
            >
              {currentDirectoryLabel}
            </div>
          </div>
          <div className="hidden truncate text-[11px] text-slate-400 xl:block">
            工作区内相对路径，不是宿主机绝对路径。
          </div>
        </div>
        <div className="flex flex-shrink-0 items-center gap-1.5">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => void handleUpload(event.target.files)}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-900"
            title={`上传文件，上限 ${Math.floor(capability.MaxUploadBytes / (1024 * 1024))}MB`}
          >
            <Upload className="h-3.5 w-3.5" />
            {uploading ? '上传中' : '上传'}
          </button>
          <button
            type="button"
            onClick={() => {
              const qs = new URLSearchParams({ Path: currentPath }).toString();
              window.open(`/agentengine/api/v1/ExportWorkspaceZip?${qs}`, '_blank', 'noopener,noreferrer');
            }}
            className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900"
            title={`导出当前目录为 ZIP`}
          >
            <Package className="h-3.5 w-3.5" />
            导出
          </button>
          {onToggleFullscreen ? (
            <button
              type="button"
              onClick={onToggleFullscreen}
              className="rounded-lg p-1.5 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-900 dark:hover:text-slate-100"
              aria-label={isFullscreen ? '退出全屏' : '全屏查看 Workspace'}
              title={isFullscreen ? '退出全屏' : '全屏查看'}
            >
              {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </button>
          ) : null}
          {onClose ? (
            <button
              type="button"
              onClick={() => {
                if (!confirmUnsaved()) return;
                onClose?.();
              }}
              className="rounded-lg p-1.5 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-900 dark:hover:text-slate-100"
              aria-label="关闭 Workspace"
              title="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <div className="flex w-full flex-col border-b border-slate-200/30 dark:border-slate-800/40 md:w-[13.5rem] md:border-b-0 md:border-r">
          <div className="flex h-14 flex-shrink-0 items-center border-b border-slate-200/30 px-3 dark:border-slate-800/40">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-xs font-medium text-slate-900 dark:text-slate-100">文件</div>
                <div className="flex items-center gap-1 text-[11px] text-slate-400">
                  <span>{entries.length} 项</span>
                  {backgroundRefreshing ? (
                    <RefreshCw className="h-3 w-3 animate-spin" aria-label="正在同步 Workspace 目录" />
                  ) : null}
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => {
                    if (!confirmUnsaved()) return;
                    void loadEntries(parentWorkspacePath(currentPath));
                  }}
                  disabled={loading || isWorkspaceRootPath(currentPath)}
                  className="rounded-md p-1.5 text-slate-500 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-900"
                  title="返回上级目录"
                >
                  <ArrowUp className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => void loadEntries(currentPath)}
                  disabled={loading}
                  className="rounded-md p-1.5 text-slate-500 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-900"
                  title="刷新目录"
                >
                  <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                </button>
              </div>
            </div>

            {error ? (
              <div className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
                {error}
              </div>
            ) : null}
          </div>

          <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto px-2 py-2">
            {!initialized && !open ? (
              <div className="px-3 py-4 text-sm text-slate-400">打开面板后加载 workspace 文件。</div>
            ) : loading ? (
              <div className="px-3 py-4 text-sm text-slate-400">正在加载目录...</div>
            ) : entries.length === 0 ? (
              <div className="px-3 py-4 text-sm text-slate-400">当前目录为空。</div>
            ) : (
              <div className="space-y-0.5">
                {entries.map((entry) => {
                  const previewKind = resolveWorkspacePreviewKind({
                    path: entry.Path,
                    mimeType: entry.MimeType,
                  }) as PreviewKind;
                  const isSelected = selectedPath === entry.Path;
                  const downloadHref =
                    entry.Type === 'file'
                      ? buildDownloadHref(entry.Path)
                      : '';

                  return (
                    <div
                      key={entry.Path}
                      title={entry.Path}
                      className={cn(
                        'group flex items-center gap-1 rounded-lg px-1.5 py-1 transition',
                        isSelected
                          ? 'bg-slate-100 text-slate-950 dark:bg-slate-800 dark:text-slate-50'
                          : 'text-slate-700 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-900',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          if (entry.Type === 'directory') {
                            void loadEntries(entry.Path);
                            return;
                          }
                          if (selectedPath !== entry.Path && !confirmUnsaved()) {
                            return;
                          }
                          setSelectedPath(entry.Path);
                        }}
                        className="flex min-w-0 flex-1 items-center gap-2 text-left"
                      >
                        <div
                          className={cn(
                            'flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-md',
                            isSelected
                              ? 'text-slate-900 dark:text-slate-50'
                              : 'text-slate-400 dark:text-slate-500',
                          )}
                        >
                          {entry.Type === 'directory' ? (
                            <FolderOpen className="h-3.5 w-3.5" />
                          ) : previewKind === 'image' ? (
                            <ImageIcon className="h-3.5 w-3.5" />
                          ) : previewKind === 'markdown' || previewKind === 'text' || previewKind === 'html' ? (
                            <FileCode2 className="h-3.5 w-3.5" />
                          ) : (
                            <FileText className="h-3.5 w-3.5" />
                          )}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[13px] leading-5">{entry.Name}</div>
                          <div
                            className={cn(
                              'truncate text-[11px] leading-4',
                              isSelected
                                ? 'text-slate-500 dark:text-slate-400'
                                : 'text-slate-400',
                            )}
                          >
                            {entry.Type === 'directory'
                              ? '目录'
                              : formatSize(entry.SizeBytes)}
                          </div>
                        </div>
                      </button>

                      <div className="flex flex-shrink-0 items-center gap-0.5 opacity-0 transition group-hover:opacity-100">
                        {entry.Type === 'file' ? (
                          <a
                            href={downloadHref}
                            download={entry.Name}
                            className={cn(
                              'rounded-md p-1.5 transition',
                              isSelected
                                ? 'hover:bg-white dark:hover:bg-slate-700'
                                : 'hover:bg-slate-100 dark:hover:bg-slate-800',
                            )}
                            title="下载"
                          >
                            <Download className="h-3.5 w-3.5" />
                          </a>
                        ) : null}
                        {capability.SupportsDelete ? (
                          <button
                            type="button"
                            onClick={() => void handleDelete(entry)}
                            className={cn(
                              'rounded-md p-1.5 transition',
                              isSelected
                                ? 'hover:bg-white dark:hover:bg-slate-700'
                                : 'hover:bg-slate-100 dark:hover:bg-slate-800',
                            )}
                            title="删除"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex h-14 flex-shrink-0 items-center border-b border-slate-200/30 px-4 dark:border-slate-800/40">
            {selectedEntry ? (
              <div className="flex w-full items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
                    {selectedEntry.Name}
                  </div>
                  <div className="mt-0.5 break-all font-mono text-[11px] text-slate-400">
                    {selectedEntry.Path}
                  </div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    {formatSize(selectedEntry.SizeBytes)}
                    {selectedEntry.MimeType ? ` · ${selectedEntry.MimeType}` : ''}
                    {selectedEntry.ModifiedAt ? ` · ${formatModifiedAt(selectedEntry.ModifiedAt)}` : ''}
                  </div>
                </div>
                {editKind === 'text' || editKind === 'code' || editKind === 'html' || editKind === 'markdown' ? (
                  <button
                    type="button"
                    onClick={() => {
                      const getContent = getContentRef.current;
                      if (getContent) {
                        void handleSave(getContent());
                      }
                    }}
                    disabled={!dirty || saving}
                    className={cn(
                      'inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition',
                      dirty && !saving
                        ? 'bg-blue-600 text-white hover:bg-blue-700'
                        : 'text-slate-400 dark:text-slate-500 cursor-not-allowed',
                    )}
                  >
                    <Save className="h-3.5 w-3.5" />
                    {saving ? '保存中' : dirty ? '保存' : '已保存'}
                  </button>
                ) : null}
              </div>
            ) : (
              <div className="w-full">
                <div className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                  文件预览
                </div>
                <div className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                  选择左侧文件后可在此查看内容。
                </div>
              </div>
            )}
          </div>

          <div
            className={cn(
              'custom-scrollbar min-h-0 flex-1',
              previewPaneIsPdf || previewPaneIsEditor ? 'overflow-hidden p-0' : 'overflow-y-auto px-4 py-4',
            )}
          >
            {renderPreview()}
          </div>
        </div>
      </div>
    </div>
  );
}
