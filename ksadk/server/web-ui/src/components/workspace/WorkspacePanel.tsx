import { useEffect, useMemo, useRef, useState } from 'react';
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
  RefreshCw,
  Trash2,
  Upload,
  X,
} from 'lucide-react';

import { cn } from '@/lib/utils';
import { MessageMarkdown } from '../MessageMarkdown';
import type { WorkspaceEntry, WorkspaceFilesCapability } from '../chat/types';
import {
  formatWorkspaceDirectoryPathLabel,
  isWorkspaceRootPath,
  normalizeWorkspacePath,
  resolveWorkspacePreviewKind,
} from '../../utils/workspace.js';

const DEFAULT_WORKSPACE_CONTENT_PATH = '/agentengine/api/v1/GetWorkspaceFileContent';

function parentWorkspacePath(path: string): string {
  if (!path || path === '.') {
    return '.';
  }
  const segments = path.split('/').filter(Boolean);
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

async function readWorkspacePayload<T>(response: globalThis.Response): Promise<T> {
  const raw = await response.text();
  let parsed: { Code?: number; Message?: string; Data?: T } | T | null = null;
  if (raw) {
    try {
      parsed = JSON.parse(raw) as { Code?: number; Message?: string; Data?: T };
    } catch {
      parsed = null;
    }
  }
  if (!response.ok) {
    throw new Error(
      parsed && typeof parsed === 'object' && 'Message' in parsed && parsed.Message
        ? parsed.Message
        : raw || `HTTP ${response.status}`,
    );
  }
  if (parsed && typeof parsed === 'object' && 'Code' in parsed) {
    if (parsed.Code !== 0) {
      throw new Error(parsed.Message || 'Workspace request failed');
    }
    return (parsed.Data ?? ({} as T)) as T;
  }
  return (parsed ?? ({} as T)) as T;
}

type WorkspacePanelProps = {
  agentId: string;
  capability: WorkspaceFilesCapability;
  open: boolean;
  onClose?: () => void;
  isFullscreen?: boolean;
  onToggleFullscreen?: () => void;
};

type PreviewKind = 'markdown' | 'text' | 'image' | 'pdf' | 'unsupported';

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

function buildDownloadHref(options: {
  agentId: string;
  contentPath: string;
  entryPath: string;
}) {
  const params = new URLSearchParams({
    AgentId: options.agentId,
    FilePath: options.entryPath,
  });
  return `${options.contentPath}?${params.toString()}`;
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
}: WorkspacePanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const previewObjectUrlRef = useRef<string | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [currentPath, setCurrentPath] = useState('.');
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [previewState, setPreviewState] = useState<PreviewState | null>(null);

  const listActionPath = capability.EntryAction
    ? `/agentengine/api/v1/${capability.EntryAction}`
    : '/agentengine/api/v1/ListWorkspaceFiles';
  const uploadActionPath = capability.UploadAction
    ? `/agentengine/api/v1/${capability.UploadAction}`
    : '/agentengine/api/v1/AddWorkspaceFile';
  const deleteActionPath = '/agentengine/api/v1/DeleteWorkspaceFile';
  const contentPath = capability.ContentPath || DEFAULT_WORKSPACE_CONTENT_PATH;

  const rootLabel = capability.RootLabel || 'Workspace';
  const displayRootLabel = rootLabel
    ? `${rootLabel.charAt(0).toUpperCase()}${rootLabel.slice(1)}`
    : 'Workspace';
  const currentDirectoryLabel = formatWorkspaceDirectoryPathLabel(currentPath);
  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.Path === selectedPath) ?? null,
    [entries, selectedPath],
  );

  const clearPreviewObjectUrl = () => {
    if (previewObjectUrlRef.current) {
      URL.revokeObjectURL(previewObjectUrlRef.current);
      previewObjectUrlRef.current = null;
    }
  };

  useEffect(() => {
    return () => clearPreviewObjectUrl();
  }, []);

  const loadEntries = async (targetPath: string) => {
    setLoading(true);
    setError('');
    try {
      const response = await fetch(listActionPath, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          AgentId: agentId,
          Path: targetPath,
          Recursive: false,
        }),
      });
      const data = await readWorkspacePayload<{
        Path?: string;
        Entries?: WorkspaceEntry[];
      }>(response);
      const nextPath = normalizeWorkspacePath(String(data?.Path || targetPath || '.'));
      const nextEntries = Array.isArray(data?.Entries) ? (data.Entries as WorkspaceEntry[]) : [];
      setCurrentPath(nextPath);
      setEntries(nextEntries);
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
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!open || initialized) {
      return;
    }
    setInitialized(true);
    void loadEntries('.');
  }, [initialized, open]);

  const loadPreview = async (entry: WorkspaceEntry) => {
    const initialKind = resolveWorkspacePreviewKind({
      path: entry.Path,
      mimeType: entry.MimeType,
    }) as PreviewKind;
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
      const response = await fetch(
        buildDownloadHref({
          agentId,
          contentPath,
          entryPath: entry.Path,
        }),
      );
      if (!response.ok) {
        throw new Error(await response.text() || `HTTP ${response.status}`);
      }
      const resolvedMimeType = response.headers.get('content-type') || entry.MimeType || '';
      const resolvedKind = resolveWorkspacePreviewKind({
        path: entry.Path,
        mimeType: resolvedMimeType,
      }) as PreviewKind;

      if (resolvedKind === 'image' || resolvedKind === 'pdf') {
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
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

      const content = await response.text();
      setPreviewState({
        path: entry.Path,
        kind: resolvedKind,
        status: 'ready',
        mimeType: resolvedMimeType,
        content,
      });
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
  };

  useEffect(() => {
    if (!selectedEntry || selectedEntry.Type !== 'file') {
      clearPreviewObjectUrl();
      setPreviewState(null);
      return;
    }
    void loadPreview(selectedEntry);
  }, [selectedEntry, agentId, contentPath]);

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
        const response = await fetch(uploadActionPath, {
          method: 'POST',
          body: formData,
        });
        await readWorkspacePayload<{ Entry?: WorkspaceEntry }>(response);
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
    if (!window.confirm(`删除 ${entry.Path} ?`)) {
      return;
    }
    setError('');
    try {
      const response = await fetch(deleteActionPath, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          AgentId: agentId,
          Path: entry.Path,
        }),
      });
      await readWorkspacePayload<{ Deleted?: boolean }>(response);
      if (selectedPath === entry.Path) {
        setSelectedPath(null);
      }
      await loadEntries(currentPath);
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
      return <MessageMarkdown content={previewState.content || ''} />;
    }
    if (previewState.kind === 'image' && previewState.objectUrl) {
      return (
        <div className="flex min-h-[16rem] items-center justify-center bg-white p-4 dark:bg-slate-950">
          <img
            src={previewState.objectUrl}
            alt={selectedEntry.Name}
            className="max-h-[calc(100vh-9rem)] max-w-full rounded-lg object-contain shadow-sm"
          />
        </div>
      );
    }
    if (previewState.kind === 'pdf' && previewState.objectUrl) {
      return (
        <iframe
          src={previewState.objectUrl}
          title={selectedEntry.Name}
          className="h-full min-h-0 w-full border-0 bg-white"
        />
      );
    }
    if (previewState.kind === 'text') {
      return (
        <pre className="custom-scrollbar overflow-x-auto bg-white p-4 font-mono text-[13px] leading-6 text-slate-800 dark:bg-slate-950 dark:text-slate-100">
          <code>{previewState.content || ''}</code>
        </pre>
      );
    }
    return (
      <PreviewEmptyState label="该文件类型暂不支持在线预览，请直接下载查看。" />
    );
  };

  const previewPaneIsPdf =
    selectedEntry?.Type === 'file'
    && previewState?.path === selectedEntry.Path
    && previewState.status === 'ready'
    && previewState.kind === 'pdf';

  return (
    <div className="flex h-full min-h-[22rem] w-full min-w-0 flex-col bg-white text-slate-800 dark:bg-slate-950 dark:text-slate-200">
      <div className="flex h-14 flex-shrink-0 items-center gap-3 border-b border-slate-200/60 px-4 dark:border-slate-800/70">
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
              onClick={onClose}
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
        <div className="flex w-full flex-col border-b border-slate-200/60 dark:border-slate-800/70 md:w-[13.5rem] md:border-b-0 md:border-r">
          <div className="flex h-14 flex-shrink-0 items-center border-b border-slate-200/60 px-3 dark:border-slate-800/70">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-xs font-medium text-slate-900 dark:text-slate-100">文件</div>
                <div className="text-[11px] text-slate-400">{entries.length} 项</div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => void loadEntries(parentWorkspacePath(currentPath))}
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
                      ? buildDownloadHref({
                          agentId,
                          contentPath,
                          entryPath: entry.Path,
                        })
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
                          ) : previewKind === 'markdown' || previewKind === 'text' ? (
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
          <div className="flex h-14 flex-shrink-0 items-center border-b border-slate-200/60 px-4 dark:border-slate-800/70">
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
                <a
                  href={buildDownloadHref({
                    agentId,
                    contentPath,
                    entryPath: selectedEntry.Path,
                  })}
                  download={selectedEntry.Name}
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900"
                >
                  <Download className="h-3.5 w-3.5" />
                  下载
                </a>
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
              previewPaneIsPdf ? 'overflow-hidden p-0' : 'overflow-y-auto px-4 py-4',
            )}
          >
            {renderPreview()}
          </div>
        </div>
      </div>
    </div>
  );
}
