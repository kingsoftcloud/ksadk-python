import React, { useEffect, useRef, useState } from 'react';
import {
  ArrowUp,
  Download,
  Folder,
  RefreshCw,
  Trash2,
  Upload,
} from 'lucide-react';

import type { WorkspaceEntry, WorkspaceFilesCapability } from '../chat/types';

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
};

export function WorkspacePanel({ agentId, capability, open }: WorkspacePanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [currentPath, setCurrentPath] = useState('.');
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const listActionPath = capability.EntryAction
    ? `/agentengine/api/v1/${capability.EntryAction}`
    : '/agentengine/api/v1/ListWorkspaceFiles';
  const uploadActionPath = capability.UploadAction
    ? `/agentengine/api/v1/${capability.UploadAction}`
    : '/agentengine/api/v1/AddWorkspaceFile';
  const deleteActionPath = '/agentengine/api/v1/DeleteWorkspaceFile';
  const contentPath = capability.ContentPath || DEFAULT_WORKSPACE_CONTENT_PATH;

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
      setCurrentPath(String(data?.Path || targetPath || '.'));
      setEntries(Array.isArray(data?.Entries) ? (data.Entries as WorkspaceEntry[]) : []);
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
        const remotePath = currentPath === '.' ? file.name : `${currentPath}/${file.name}`;
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
      await loadEntries(currentPath);
    } catch (deleteError) {
      console.error('Failed to delete workspace file:', deleteError);
      setError(deleteError instanceof Error ? deleteError.message : String(deleteError));
    }
  };

  return (
    <div className="flex h-full min-h-[22rem] flex-col bg-white text-slate-800 dark:bg-slate-950 dark:text-slate-200">
      <div className="border-b border-slate-200 px-4 py-4 dark:border-slate-800">
        <div className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-400">
          {capability.RootLabel}
        </div>
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={() => void loadEntries(parentWorkspacePath(currentPath))}
            disabled={loading || currentPath === '.'}
            className="rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
          >
            <ArrowUp className="h-4 w-4" />
          </button>
          <div className="min-w-0 flex-1 truncate rounded-lg bg-slate-100 px-3 py-2 text-sm font-medium dark:bg-slate-900">
            {currentPath}
          </div>
          <button
            type="button"
            onClick={() => void loadEntries(currentPath)}
            disabled={loading}
            className="rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
        <div className="mt-3 flex items-center gap-2">
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
            className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
          >
            <Upload className="h-4 w-4" />
            {uploading ? '上传中' : '上传文件'}
          </button>
          <div className="text-xs text-slate-400">
            上限 {Math.floor(capability.MaxUploadBytes / (1024 * 1024))}MB
          </div>
        </div>
        {error ? (
          <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </div>
        ) : null}
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {!initialized && !open ? (
          <div className="text-sm text-slate-400">打开面板后加载 workspace 文件。</div>
        ) : loading ? (
          <div className="text-sm text-slate-400">正在加载目录...</div>
        ) : entries.length === 0 ? (
          <div className="text-sm text-slate-400">当前目录为空。</div>
        ) : (
          <div className="space-y-2">
            {entries.map((entry) => {
              const downloadHref =
                entry.Type === 'file'
                  ? `${contentPath}?${new URLSearchParams({
                      AgentId: agentId,
                      FilePath: entry.Path,
                    }).toString()}`
                  : '';
              return (
                <div
                  key={entry.Path}
                  className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-3 dark:border-slate-800 dark:bg-slate-900"
                >
                  <div className="flex items-start gap-3">
                    <button
                      type="button"
                      onClick={() => {
                        if (entry.Type === 'directory') {
                          void loadEntries(entry.Path);
                        }
                      }}
                      className="mt-0.5 rounded-lg p-1 text-slate-500 transition hover:bg-slate-100 dark:hover:bg-slate-800"
                    >
                      <Folder className="h-4 w-4" />
                    </button>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                        {entry.Path}
                      </div>
                      <div className="mt-1 text-xs text-slate-400">
                        {entry.Type === 'directory'
                          ? '目录'
                          : `${entry.SizeBytes ?? 0} bytes${entry.MimeType ? ` · ${entry.MimeType}` : ''}`}
                      </div>
                      {entry.ModifiedAt ? (
                        <div className="mt-1 text-[11px] text-slate-400">
                          {formatModifiedAt(entry.ModifiedAt)}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex items-center gap-1">
                      {entry.Type === 'file' ? (
                        <a
                          href={downloadHref}
                          download={entry.Name}
                          className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 dark:hover:bg-slate-800"
                          title="下载"
                        >
                          <Download className="h-4 w-4" />
                        </a>
                      ) : null}
                      {capability.SupportsDelete ? (
                        <button
                          type="button"
                          onClick={() => void handleDelete(entry)}
                          className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 dark:hover:bg-slate-800"
                          title="删除"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      ) : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
