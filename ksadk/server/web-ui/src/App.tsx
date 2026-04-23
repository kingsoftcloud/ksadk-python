import React, { useEffect, useRef, useState } from 'react';

import { buildComposerContextIndicator } from './utils/context.js';
import { resolveComposerMaxHeight, resolveSidebarVisibility } from './utils/mobile-layout.js';
import {
  readPersistedSessionId,
  resolveSessionToRestore,
  writePersistedSessionId,
} from './utils/session.js';
import { useResponsiveViewport } from './hooks/useResponsiveViewport';
import { cn } from '@/lib/utils';
import { AttachmentPreview } from './components/chat/AttachmentPreview';
import { ChatComposer } from './components/chat/ChatComposer';
import { ChatHeader } from './components/chat/ChatHeader';
import { ChatMessageList } from './components/chat/ChatMessageList';
import { ChatSidebar } from './components/chat/ChatSidebar';
import { WorkspacePanel } from './components/workspace/WorkspacePanel';
import type {
  Message,
  MessageAttachment,
  ModelCatalogItem,
  PreviewImageSize,
  Session,
  WorkspaceFilesCapability,
} from './components/chat/types';
import { Sheet, SheetContent, SheetTitle } from './components/ui/sheet';

type SessionEventRecord = {
  EventId?: string;
  EventType?: string;
  Content?: {
    role?: string;
    status?: string;
    detail?: string;
    parts?: Array<{
      type?: string;
      text?: string;
      inlineData?: {
        displayName?: string;
        mimeType?: string;
        data?: string;
      };
      fileData?: {
        fileUri?: string;
        displayName?: string;
        mimeType?: string;
      };
    }>;
  };
  Timestamp?: number;
  Metadata?: Record<string, unknown>;
  SeqId?: number;
};

type ParsedMessageContent = {
  text: string;
  attachments?: MessageAttachment[];
};

type CompactionStreamPayload = {
  phase?: 'start' | 'done' | 'failed';
  trigger?: 'auto' | 'prompt_too_long';
  compacted_until_seq_id?: number;
  timestamp?: number;
};

type BootstrapModel = ModelCatalogItem & {
  source?: string;
};

type BootstrapWorkspaceFiles = WorkspaceFilesCapability;

type RuntimeApiFormat = 'responses' | 'chat_completions';

type AgentInputPart =
  | {
      type: 'input_text';
      text: string;
    }
  | {
      type: 'input_file';
      fileData: {
        fileUri: string;
        displayName: string;
        mimeType: string;
      };
    };

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function fileFingerprint(file: File): string {
  return [file.name, file.size, file.lastModified, file.type].join(':');
}

function resolveRunAgentApiFormat(options: {
  agentFramework: string;
  apiFormats: RuntimeApiFormat[];
}): RuntimeApiFormat {
  const { agentFramework, apiFormats } = options;
  if (agentFramework === 'hermes' && apiFormats.includes('chat_completions')) {
    return 'chat_completions';
  }
  if (apiFormats.includes('responses')) {
    return 'responses';
  }
  if (apiFormats.includes('chat_completions')) {
    return 'chat_completions';
  }
  return 'responses';
}

function normalizeApiFormats(value: unknown): RuntimeApiFormat[] {
  if (!Array.isArray(value)) {
    return ['responses', 'chat_completions'];
  }
  const formats = value.filter(
    (item): item is RuntimeApiFormat => item === 'responses' || item === 'chat_completions',
  );
  return formats.length > 0 ? formats : ['responses', 'chat_completions'];
}

function textFromUnknown(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && 'text' in item && typeof item.text === 'string') {
          return item.text;
        }
        return '';
      })
      .join('');
  }
  return '';
}

function extractChatCompletionsStreamDelta(data: any): {
  content: string;
  reasoning: string;
  finalText: string;
} {
  const delta = data?.choices?.[0]?.delta;
  const message = data?.choices?.[0]?.message;
  return {
    content: delta ? textFromUnknown(delta.content) : '',
    reasoning: delta ? textFromUnknown(delta.reasoning_content) : '',
    finalText: message ? textFromUnknown(message.content) : '',
  };
}

function mergeAttachmentFiles(current: File[], incoming: File[]): File[] {
  const merged = new Map<string, File>();
  for (const file of current) {
    merged.set(fileFingerprint(file), file);
  }
  for (const file of incoming) {
    merged.set(fileFingerprint(file), file);
  }
  return Array.from(merged.values());
}

function extractClipboardFiles(event: React.ClipboardEvent<HTMLTextAreaElement>): File[] {
  return Array.from(event.clipboardData.items || [])
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
}

function upsertModelOptions(
  current: ModelCatalogItem[],
  incoming: ModelCatalogItem[],
): ModelCatalogItem[] {
  const merged = new Map<string, ModelCatalogItem>();
  for (const item of current) {
    if (!item?.id) continue;
    merged.set(item.id, item);
  }
  for (const item of incoming) {
    if (!item?.id) continue;
    merged.set(item.id, { ...(merged.get(item.id) || {}), ...item });
  }
  return Array.from(merged.values()).sort((left, right) => left.id.localeCompare(right.id));
}

function sessionUpdatedAtValue(session: Session): number {
  const raw = session.UpdatedAt;
  if (typeof raw === 'string') {
    const parsed = Date.parse(raw);
    return Number.isNaN(parsed) ? 0 : parsed;
  }
  if (typeof raw === 'number') {
    return raw;
  }
  return 0;
}

function upsertSessions(current: Session[], incoming: Session[]): Session[] {
  const merged = new Map<string, Session>();
  for (const session of current) {
    if (!session?.SessionId) continue;
    merged.set(session.SessionId, session);
  }
  for (const session of incoming) {
    if (!session?.SessionId) continue;
    merged.set(session.SessionId, { ...(merged.get(session.SessionId) || {}), ...session });
  }
  return Array.from(merged.values()).sort(
    (left, right) => sessionUpdatedAtValue(right) - sessionUpdatedAtValue(left),
  );
}

function sessionTitle(session: Session): string {
  const title = String(session.Title || '').trim();
  if (title) {
    return title;
  }
  const firstPrompt = String(session.FirstPrompt || '').trim();
  if (firstPrompt) {
    return firstPrompt;
  }
  return '新对话';
}

function attachmentContentUrl(fileUri?: string): string {
  const normalized = String(fileUri || '').trim();
  if (!normalized) {
    return '';
  }
  return `/agentengine/api/v1/AttachmentContent?FileUri=${encodeURIComponent(normalized)}`;
}

function parseMessageContent(event: SessionEventRecord): ParsedMessageContent {
  const parts = event.Content?.parts || [];
  const textSegments: string[] = [];
  const attachmentsByKey = new Map<string, MessageAttachment>();

  const pushAttachment = (attachment: MessageAttachment) => {
    const key = `${attachment.fileUri || attachment.url || attachment.name}|${attachment.type}`;
    if (!attachmentsByKey.has(key)) {
      attachmentsByKey.set(key, attachment);
    }
  };

  for (const part of parts) {
    if (part?.type === 'input_text' || part?.text) {
      textSegments.push(part.text || '');
      continue;
    }
    if (part?.type === 'input_file' && part.inlineData) {
      pushAttachment({
        name: part.inlineData.displayName || 'attachment',
        url: `data:${part.inlineData.mimeType || 'application/octet-stream'};base64,${part.inlineData.data}`,
        type: part.inlineData.mimeType || 'application/octet-stream',
      });
      continue;
    }
    if (part?.type === 'input_file' && part.fileData) {
      const fileUri = String(part.fileData.fileUri || '').trim();
      pushAttachment({
        name: part.fileData.displayName || 'attachment',
        url: attachmentContentUrl(fileUri),
        type: part.fileData.mimeType || 'application/octet-stream',
        fileUri,
      });
    }
  }

  const metadataAttachments = Array.isArray(event.Metadata?.attachments)
    ? event.Metadata.attachments
    : [];

  for (const attachment of metadataAttachments) {
    const fileUri = String(attachment.file_uri || '').trim();
    pushAttachment({
      name: attachment.display_name || 'attachment',
      url: attachmentContentUrl(fileUri),
      type: attachment.mime_type || 'application/octet-stream',
      fileUri,
    });
  }

  const attachments = Array.from(attachmentsByKey.values());
  return {
    text: textSegments.join(''),
    attachments: attachments.length > 0 ? attachments : undefined,
  };
}

function buildCompactionLabel(trigger?: string, status?: Message['status'], historical?: boolean) {
  if (status === 'running') {
    return trigger === 'prompt_too_long'
      ? '检测到上下文过长，正在自动压缩历史后重试'
      : '正在自动压缩上下文';
  }
  if (historical) {
    return trigger === 'prompt_too_long'
      ? '上下文过长，系统已自动压缩历史并重试'
      : '系统已自动压缩较早的对话上下文';
  }
  if (status === 'failed') {
    return '自动压缩上下文未完成';
  }
  return trigger === 'prompt_too_long'
    ? '已完成上下文压缩，并继续当前回复'
    : '已完成上下文压缩';
}

function buildCompactionMessage(options: {
  id: string;
  timestamp: number;
  status: Message['status'];
  trigger?: string;
  compactedUntilSeqId?: number;
  summary?: string;
  historical?: boolean;
}): Message {
  return {
    id: options.id,
    role: 'system',
    eventType: 'context_checkpoint',
    status: options.status,
    trigger: options.trigger,
    compactedUntilSeqId: options.compactedUntilSeqId,
    summary: options.summary,
    historical: options.historical,
    timestamp: options.timestamp,
    content: buildCompactionLabel(options.trigger, options.status, options.historical),
  };
}

function buildMessageFromSessionEvent(event: SessionEventRecord): Message | null {
  const eventType = event.EventType || '';
  if (eventType === 'run_status') {
    if (event.Content?.status === 'failed') {
      return {
        id: event.EventId || String(Date.now() + Math.random()),
        role: 'system',
        content: event.Content?.detail || '本轮运行失败。',
        eventType,
        status: 'failed',
        timestamp: event.Timestamp || Date.now(),
      };
    }
    return null;
  }

  if (eventType === 'context_checkpoint') {
    const parsed = parseMessageContent(event);
    return buildCompactionMessage({
      id: event.EventId || String(Date.now() + Math.random()),
      timestamp: event.Timestamp || Date.now(),
      status: 'completed',
      trigger: String(event.Metadata?.trigger || 'auto'),
      compactedUntilSeqId: Number(event.Metadata?.compacted_until_seq_id || 0) || undefined,
      summary: parsed.text || undefined,
      historical: true,
    });
  }

  if (eventType !== 'user_message' && eventType !== 'assistant_message') {
    return null;
  }

  const parsed = parseMessageContent(event);
  if (!parsed.text && !parsed.attachments?.length) {
    return null;
  }

  return {
    id: event.EventId || String(Date.now() + Math.random()),
    role: eventType === 'user_message' ? 'user' : 'model',
    content: parsed.text,
    timestamp: event.Timestamp || Date.now(),
    eventType,
    attachments: parsed.attachments,
  };
}

function formatDate(ts?: string | number | null) {
  if (!ts) return '';
  if (typeof ts === 'string') {
    const parsed = Date.parse(ts);
    return Number.isNaN(parsed)
      ? ''
      : new Date(parsed).toLocaleString('zh-CN', {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        });
  }
  const date = new Date(ts > 1e11 ? ts : ts * 1000);
  return date.toLocaleString('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function App() {
  const [agentId, setAgentId] = useState('default-agent');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<File[]>([]);
  const [previewAttachment, setPreviewAttachment] = useState<MessageAttachment | null>(null);
  const [previewImageSize, setPreviewImageSize] = useState<PreviewImageSize | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [mobileActionsOpen, setMobileActionsOpen] = useState(false);
  const [agentName, setAgentName] = useState('AgentEngine');
  const [selectedModel, setSelectedModel] = useState('');
  const [availableModels, setAvailableModels] = useState<ModelCatalogItem[]>([]);
  const [modelSource, setModelSource] = useState('');
  const [modelCatalogLoaded, setModelCatalogLoaded] = useState(false);
  const [agentFramework, setAgentFramework] = useState('');
  const [workspaceFiles, setWorkspaceFiles] = useState<BootstrapWorkspaceFiles | null>(null);
  const [accessMode, setAccessMode] = useState('Owner');
  const [workspacePanelOpen, setWorkspacePanelOpen] = useState(false);
  const [apiFormats, setApiFormats] = useState<RuntimeApiFormat[]>([
    'responses',
    'chat_completions',
  ]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const activeCompactionMessageIdRef = useRef<string | null>(null);
  const currentSessionIdRef = useRef<string | null>(null);

  const { isMobile, viewportHeight } = useResponsiveViewport();
  const composerMaxHeight = resolveComposerMaxHeight({ isMobile, viewportHeight });
  const { desktopSidebarVisible } = resolveSidebarVisibility({
    isMobile,
    desktopSidebarOpen: sidebarOpen,
    mobileSidebarOpen,
  });

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isStreaming]);

  useEffect(() => {
    void fetchBootstrap();
    // fetchBootstrap intentionally runs once on initial mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!textareaRef.current) {
      return;
    }
    textareaRef.current.style.height = 'auto';
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, composerMaxHeight)}px`;
  }, [input, composerMaxHeight]);

  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
    writePersistedSessionId(agentId, currentSessionId);
  }, [agentId, currentSessionId]);

  useEffect(() => {
    if (!isMobile) {
      setMobileSidebarOpen(false);
      setMobileActionsOpen(false);
    }
  }, [isMobile]);

  const appendAttachments = (incoming: File[]) => {
    if (!incoming.length) {
      return;
    }
    setAttachments((prev) => mergeAttachmentFiles(prev, incoming));
  };

  const handleInputChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(event.target.value);
    event.target.style.height = 'auto';
    event.target.style.height = `${Math.min(event.target.scrollHeight, composerMaxHeight)}px`;
  };

  const openAttachmentPreview = (attachment: MessageAttachment) => {
    setPreviewAttachment(attachment);
    setPreviewImageSize(null);
  };

  const closeAttachmentPreview = () => {
    setPreviewAttachment(null);
    setPreviewImageSize(null);
  };

  const handleComposerPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const pastedFiles = extractClipboardFiles(event);
    if (!pastedFiles.length) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    appendAttachments(pastedFiles);
  };

  const fetchModels = async (targetAgentId: string) => {
    try {
      const response = await fetch('/agentengine/api/v1/ListAgentModels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ AgentId: targetAgentId }),
      });
      const data = await response.json();
      const models = data?.Data?.Models;
      if (Array.isArray(models)) {
        setAvailableModels((current) => upsertModelOptions(current, models));
      }
      if (data?.Data?.Current) {
        setSelectedModel(data.Data.Current);
      }
      if (data?.Data?.Source) {
        setModelSource(String(data.Data.Source));
      }
    } catch (error) {
      console.error('Failed to fetch models:', error);
    } finally {
      setModelCatalogLoaded(true);
    }
  };

  const loadSession = async (sessionId: string) => {
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    activeCompactionMessageIdRef.current = null;
    if (isMobile) {
      setMobileSidebarOpen(false);
    }

    try {
      const response = await fetch('/agentengine/api/v1/ListSessionEvents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ SessionId: sessionId }),
      });
      const data = await response.json();
      if (data?.Data?.Events) {
        const history = data.Data.Events
          .map((event: SessionEventRecord) => buildMessageFromSessionEvent(event))
          .filter((event: Message | null): event is Message => Boolean(event));
        setMessages(history);
      } else {
        setMessages([]);
      }
    } catch (error) {
      console.error('Failed to load session events:', error);
    }
  };

  const fetchSessions = async (
    targetAgentId = 'default-agent',
    preferredSessionId: string | null = null,
  ) => {
    try {
      const response = await fetch('/agentengine/api/v1/ListSessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ AgentId: targetAgentId }),
      });
      const data = await response.json();
      if (data?.Data?.Sessions) {
        const sorted = upsertSessions([], data.Data.Sessions as Session[]);
        setSessions(sorted);
        const activeSessionId = currentSessionIdRef.current;
        const restoredSessionId = resolveSessionToRestore(
          sorted,
          activeSessionId || preferredSessionId || readPersistedSessionId(targetAgentId),
        );
        if (restoredSessionId && restoredSessionId !== activeSessionId) {
          void loadSession(restoredSessionId);
        } else if (!restoredSessionId && activeSessionId) {
          currentSessionIdRef.current = null;
          setCurrentSessionId(null);
          setMessages([]);
        }
      }
    } catch (error) {
      console.error('Failed to fetch sessions:', error);
    }
  };

  const fetchBootstrap = async () => {
    try {
      const response = await fetch('/agentengine/api/v1/GetAgentUiBootstrap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await response.json();
      const bootstrapAgentId = data?.Data?.Agent?.AgentId || 'default-agent';
      setAgentId(bootstrapAgentId);
      if (data?.Data?.Agent?.Name) {
        setAgentName(data.Data.Agent.Name);
      }
      setAgentFramework(String(data?.Data?.Agent?.Framework || '').trim().toLowerCase());
      setApiFormats(normalizeApiFormats(data?.Data?.ApiFormats));
      setAccessMode(String(data?.Data?.AccessMode || 'Owner'));

      const bootstrapWorkspaceFiles =
        data?.Data?.Capabilities?.WorkspaceFiles && data?.Data?.WorkspaceFiles?.Enabled
          ? (data.Data.WorkspaceFiles as BootstrapWorkspaceFiles)
          : null;
      setWorkspaceFiles(bootstrapWorkspaceFiles);
      if (!bootstrapWorkspaceFiles) {
        setWorkspacePanelOpen(false);
      }

      void fetchSessions(bootstrapAgentId, readPersistedSessionId(bootstrapAgentId));

      const bootstrapModel: BootstrapModel | undefined = data?.Data?.Model;
      if (bootstrapModel?.id) {
        setSelectedModel(bootstrapModel.id);
        setAvailableModels((current) => upsertModelOptions(current, [bootstrapModel]));
        setModelSource(bootstrapModel.source || '');
      }
      void fetchModels(bootstrapAgentId);
    } catch (error) {
      console.error('Failed to fetch bootstrap:', error);
      void fetchSessions('default-agent', readPersistedSessionId('default-agent'));
      void fetchModels('default-agent');
    }
  };

  const createNewSession = async () => {
    if (isStreaming) return;

    try {
      const response = await fetch('/agentengine/api/v1/CreateSession', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ AgentId: agentId }),
      });
      const data = await response.json();
      const newId = data?.Data?.Session?.SessionId;
      if (newId) {
        setSessions((prev) =>
          upsertSessions(prev, [{ SessionId: newId, UpdatedAt: new Date().toISOString() }]),
        );
        currentSessionIdRef.current = newId;
        setCurrentSessionId(newId);
        setMessages([]);
        if (isMobile) {
          setMobileSidebarOpen(false);
          setMobileActionsOpen(false);
        }
        void fetchSessions(agentId, newId);
      }
    } catch (error) {
      console.error('Failed to create session:', error);
    }
  };

  const stopGeneration = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
      setIsStreaming(false);
    }
  };

  const upsertCompactionMessage = (payload: CompactionStreamPayload) => {
    const currentId = activeCompactionMessageIdRef.current || `compaction-${Date.now()}`;
    if (!activeCompactionMessageIdRef.current) {
      activeCompactionMessageIdRef.current = currentId;
    }

    const nextStatus: Message['status'] =
      payload.phase === 'start'
        ? 'running'
        : payload.phase === 'failed'
          ? 'failed'
          : 'completed';
    const nextMessage = buildCompactionMessage({
      id: currentId,
      timestamp: payload.timestamp || Date.now(),
      status: nextStatus,
      trigger: payload.trigger,
      compactedUntilSeqId: payload.compacted_until_seq_id,
    });

    setMessages((prev) => {
      const existingIndex = prev.findIndex((message) => message.id === currentId);
      if (existingIndex < 0) {
        return [...prev, nextMessage];
      }
      return prev.map((message) => (message.id === currentId ? { ...message, ...nextMessage } : message));
    });

    if (nextStatus !== 'running') {
      activeCompactionMessageIdRef.current = null;
    }
  };

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if ((!input.trim() && attachments.length === 0) || isStreaming) return;

    let sessionId = currentSessionId;
    if (!sessionId) {
      try {
        const sessionResponse = await fetch('/agentengine/api/v1/CreateSession', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ AgentId: agentId }),
        });
        const sessionPayload = await sessionResponse.json();
        sessionId = sessionPayload?.Data?.Session?.SessionId || null;
      } catch (error) {
        console.error('Failed to create session before RunAgent:', error);
      }
    }

    if (!sessionId) {
      sessionId = `default-session-${Date.now()}`;
    }

    setSessions((prev) =>
      upsertSessions(prev, [{ SessionId: sessionId, UpdatedAt: new Date().toISOString() }]),
    );
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);

    const userText = input.trim();
    setInput('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }

    const messageAttachments = attachments.map((file) => ({
      name: file.name,
      url: URL.createObjectURL(file),
      type: file.type || 'application/octet-stream',
    }));

    const userMessage: Message = {
      id: String(Date.now()),
      role: 'user',
      content: userText,
      timestamp: Date.now(),
      attachments: messageAttachments.length > 0 ? messageAttachments : undefined,
    };

    setMessages((prev) => [...prev, userMessage]);
    setIsStreaming(true);
    setMobileActionsOpen(false);

    const parts: AgentInputPart[] = [{ type: 'input_text', text: userText }];

    for (const file of attachments) {
      if (file.size > 100 * 1024 * 1024) {
        setMessages((prev) => [
          ...prev,
          {
            id: String(Date.now()),
            role: 'model',
            content: `【系统提示】文件 ${file.name} 超过 100MB 限制，未发送。`,
            timestamp: Date.now(),
          },
        ]);
        continue;
      }

      const formData = new FormData();
      formData.append('file', file);

      try {
        const uploadResponse = await fetch('/agentengine/api/v1/UploadFile', {
          method: 'POST',
          body: formData,
        });
        if (!uploadResponse.ok) {
          const errorText = await uploadResponse.text();
          throw new Error(`HTTP ${uploadResponse.status}: ${errorText}`);
        }

        const uploadData = await uploadResponse.json();
        if (uploadData?.Data?.FileData) {
          parts.push({
            type: 'input_file',
            fileData: {
              fileUri: uploadData.Data.FileData.fileUri,
              displayName: uploadData.Data.FileData.displayName || file.name,
              mimeType:
                uploadData.Data.FileData.mimeType || file.type || 'application/octet-stream',
            },
          });
        } else if (uploadData?.Message !== 'Success') {
          throw new Error(`服务端返回异常: ${uploadData?.Message || JSON.stringify(uploadData)}`);
        }
      } catch (error: unknown) {
        console.error('Upload failed', error);
        setMessages((prev) => [
          ...prev,
          {
            id: String(Date.now()),
            role: 'model',
            content: `【系统提示】文件 ${file.name} 上传失败，原因: ${getErrorMessage(error)}`,
            timestamp: Date.now(),
          },
        ]);
      }
    }

    setAttachments([]);
    abortControllerRef.current = new AbortController();
    const runAgentApiFormat = resolveRunAgentApiFormat({ agentFramework, apiFormats });

    try {
      const response = await fetch('/agentengine/api/v1/RunAgent', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({
          AgentId: agentId,
          SessionId: sessionId,
          Stream: true,
          ApiFormat: runAgentApiFormat,
          Model: selectedModel || undefined,
          ModelMetadata: selectedModelMetadata || undefined,
          Messages: [
            {
              role: 'user',
              content: parts,
            },
          ],
        }),
        signal: abortControllerRef.current.signal,
      });

      if (!response.body) throw new Error('No readable stream');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      const assistantMessageId = String(Date.now() + 1);
      let assistantMessageCreated = false;
      const ensureAssistantMessage = () => {
        if (assistantMessageCreated) return;
        assistantMessageCreated = true;
        setMessages((prev) => [
          ...prev,
          { id: assistantMessageId, role: 'model', content: '', timestamp: Date.now(), reasoning: '' },
        ]);
      };

      let buffer = '';
      let isDone = false;

      while (!isDone) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';

        for (const chunk of chunks) {
          if (!chunk.trim()) continue;

          let currentEvent = 'message';
          const dataLines: string[] = [];
          for (const line of chunk.split('\n')) {
            if (line.startsWith('event:')) {
              currentEvent = line.substring(6).trim() || 'message';
            } else if (line.startsWith('data:')) {
              dataLines.push(line.substring(5).trim());
            }
          }

          const dataString = dataLines.join('\n').trim();
          if (dataString === '[DONE]') {
            isDone = true;
            break;
          }
          if (!dataString) continue;

          try {
            const data = JSON.parse(dataString);
            const chatDelta = extractChatCompletionsStreamDelta(data);
            if (chatDelta.reasoning) {
              ensureAssistantMessage();
              setMessages((prev) =>
                prev.map((message) =>
                  message.id === assistantMessageId
                    ? { ...message, reasoning: (message.reasoning || '') + chatDelta.reasoning }
                    : message,
                ),
              );
            }
            if (chatDelta.content) {
              ensureAssistantMessage();
              setMessages((prev) =>
                prev.map((message) =>
                  message.id === assistantMessageId
                    ? { ...message, content: message.content + chatDelta.content }
                    : message,
                ),
              );
            }
            if (chatDelta.finalText) {
              ensureAssistantMessage();
              setMessages((prev) =>
                prev.map((message) =>
                  message.id === assistantMessageId
                    ? { ...message, content: chatDelta.finalText }
                    : message,
                ),
              );
            }
            if (chatDelta.reasoning || chatDelta.content || chatDelta.finalText) {
              continue;
            }

            if (currentEvent === 'response.compaction.start') {
              upsertCompactionMessage({ ...data, phase: 'start' });
              continue;
            }
            if (currentEvent === 'response.compaction.done') {
              upsertCompactionMessage({ ...data, phase: 'done' });
              continue;
            }
            if (currentEvent === 'response.compaction.failed') {
              upsertCompactionMessage({ ...data, phase: 'failed' });
              continue;
            }

            if (currentEvent === 'response.tool_call') {
              ensureAssistantMessage();
              setMessages((prev) =>
                prev.map((message) => {
                  if (message.id === assistantMessageId) {
                    const name = String(data.name || 'tool');
                    const args =
                      typeof data.args === 'object'
                        ? JSON.stringify(data.args, null, 2)
                        : String(data.args || '');
                    return {
                      ...message,
                      tools: {
                        ...(message.tools || {}),
                        [name]: { name, args, status: 'running' },
                      },
                    };
                  }
                  return message;
                }),
              );
            } else if (currentEvent === 'response.tool_result') {
              ensureAssistantMessage();
              setMessages((prev) =>
                prev.map((message) => {
                  if (message.id === assistantMessageId) {
                    const name = String(data.name || 'tool');
                    const output =
                      typeof data.output === 'object'
                        ? JSON.stringify(data.output, null, 2)
                        : String(data.output || '');
                    return {
                      ...message,
                      tools: {
                        ...(message.tools || {}),
                        [name]: {
                          ...(message.tools?.[name] || { name, args: '' }),
                          output,
                          status: 'completed',
                        },
                      },
                    };
                  }
                  return message;
                }),
              );
            } else if (currentEvent === 'response.reasoning.delta') {
              const delta = data.delta || '';
              if (delta) {
                ensureAssistantMessage();
                setMessages((prev) =>
                  prev.map((message) =>
                    message.id === assistantMessageId
                      ? { ...message, reasoning: (message.reasoning || '') + delta }
                      : message,
                  ),
                );
              }
            } else if (currentEvent === 'response.output_text.delta') {
              const delta = data.delta || '';
              if (delta) {
                ensureAssistantMessage();
                setMessages((prev) =>
                  prev.map((message) =>
                    message.id === assistantMessageId
                      ? { ...message, content: message.content + delta }
                      : message,
                  ),
                );
              }
            } else if (currentEvent === 'response.completed') {
              const finalText = data.output_text || '';
              if (finalText) {
                ensureAssistantMessage();
                setMessages((prev) =>
                  prev.map((message) =>
                    message.id === assistantMessageId ? { ...message, content: finalText } : message,
                  ),
                );
              }
            } else if (data.content?.parts?.[0]?.text) {
              const delta = data.content.parts[0].text;
              if (!data.actions?.finishReason) {
                ensureAssistantMessage();
                setMessages((prev) =>
                  prev.map((message) =>
                    message.id === assistantMessageId
                      ? { ...message, content: message.content + delta }
                      : message,
                  ),
                );
              }
            }
          } catch (error) {
            console.warn('Failed to parse SSE data', dataString, error);
          }
        }
      }
    } catch (error: unknown) {
      const isAbortError = error instanceof DOMException && error.name === 'AbortError';
      if (isAbortError) {
        if (activeCompactionMessageIdRef.current) {
          upsertCompactionMessage({ phase: 'failed' });
        }
        console.log('Stream aborted');
      } else {
        if (activeCompactionMessageIdRef.current) {
          upsertCompactionMessage({ phase: 'failed' });
        }
        console.error('SSE Error:', error);
        setMessages((prev) => [
          ...prev,
          {
            id: String(Date.now()),
            role: 'model',
            content: isAbortError ? '连接已中断。' : '连接断开或生成出错。',
            timestamp: Date.now(),
          },
        ]);
      }
    } finally {
      setIsStreaming(false);
      abortControllerRef.current = null;
      activeCompactionMessageIdRef.current = null;
      void fetchSessions(agentId, sessionId);
    }
  };

  const deleteSession = async (
    sessionId: string,
    event: React.MouseEvent<HTMLButtonElement>,
  ) => {
    event.stopPropagation();
    try {
      await fetch('/agentengine/api/v1/DeleteSession', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ SessionId: sessionId }),
      });
      setSessions((prev) => prev.filter((session) => session.SessionId !== sessionId));
      if (currentSessionId === sessionId) {
        currentSessionIdRef.current = null;
        setMessages([]);
        setCurrentSessionId(null);
        void fetchSessions(agentId);
      }
    } catch (error) {
      console.error('Failed to delete session', error);
    }
  };

  const selectedModelMetadata =
    availableModels.find((model) => model.id === selectedModel) || null;
  const selectedModelLabel = selectedModelMetadata?.display_name || selectedModel || '';
  const composerContextIndicator = buildComposerContextIndicator({
    messages,
    draftInput: input,
    selectedModel: selectedModelMetadata,
  });
  const workspaceEnabled = Boolean(workspaceFiles?.Enabled && accessMode === 'Owner');

  return (
    <div className="flex h-[var(--app-height)] min-h-[var(--app-height)] overflow-hidden bg-white font-sans text-slate-800 dark:bg-slate-900 dark:text-slate-200">
      {!isMobile ? (
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
            formatDate={formatDate}
            sessionTitle={sessionTitle}
          />
        </aside>
      ) : (
        <Sheet open={mobileSidebarOpen} onOpenChange={setMobileSidebarOpen}>
          <SheetContent
            side="left"
            className="w-[88vw] max-w-sm border-slate-200 bg-slate-50 p-0 dark:border-slate-800 dark:bg-slate-950"
          >
            <SheetTitle className="sr-only">历史记录</SheetTitle>
            <ChatSidebar
              sessions={sessions}
              currentSessionId={currentSessionId}
              isStreaming={isStreaming}
              onCreateNewSession={createNewSession}
              onSelectSession={loadSession}
              onDeleteSession={deleteSession}
              formatDate={formatDate}
              sessionTitle={sessionTitle}
            />
          </SheetContent>
        </Sheet>
      )}

      <main className="relative flex min-w-0 flex-1 flex-col bg-white dark:bg-slate-900">
        <ChatHeader
          agentName={agentName}
          currentSessionId={currentSessionId}
          isMobile={isMobile}
          sidebarOpen={sidebarOpen}
          mobileSidebarOpen={mobileSidebarOpen}
          onToggleSidebar={() => {
            if (isMobile) {
              setMobileSidebarOpen((prev) => !prev);
            } else {
              setSidebarOpen((prev) => !prev);
            }
          }}
          availableModels={availableModels}
          selectedModel={selectedModel}
          onSelectModel={(modelId) => {
            setSelectedModel(modelId);
            if (isMobile) {
              setMobileActionsOpen(false);
            }
          }}
          selectedModelLabel={selectedModelLabel}
          modelCatalogLoaded={modelCatalogLoaded}
          modelSource={modelSource}
          mobileActionsOpen={mobileActionsOpen}
          onMobileActionsOpenChange={setMobileActionsOpen}
          workspaceEnabled={workspaceEnabled}
          onOpenWorkspace={() => setWorkspacePanelOpen(true)}
        />

        <ChatMessageList
          agentName={agentName}
          isMobile={isMobile}
          isStreaming={isStreaming}
          messages={messages}
          onOpenAttachmentPreview={openAttachmentPreview}
          scrollRef={scrollRef}
        />

        <ChatComposer
          attachments={attachments}
          composerContextIndicator={composerContextIndicator}
          composerMaxHeight={composerMaxHeight}
          fileInputRef={fileInputRef}
          input={input}
          isMobile={isMobile}
          isStreaming={isStreaming}
          onAppendAttachments={appendAttachments}
          onInputChange={handleInputChange}
          onPaste={handleComposerPaste}
          onRemoveAttachment={(index) =>
            setAttachments((prev) => prev.filter((_, attachmentIndex) => attachmentIndex !== index))
          }
          onStopGeneration={stopGeneration}
          onSubmit={handleSubmit}
          textareaRef={textareaRef}
        />
      </main>

      <AttachmentPreview
        attachment={previewAttachment}
        isMobile={isMobile}
        previewImageSize={previewImageSize}
        onClose={closeAttachmentPreview}
        onImageLoad={setPreviewImageSize}
      />

      {workspaceEnabled && workspaceFiles ? (
        <Sheet open={workspacePanelOpen} onOpenChange={setWorkspacePanelOpen}>
          <SheetContent
            side={isMobile ? 'bottom' : 'right'}
            className={cn(
              'border-slate-200 bg-white p-0 dark:border-slate-800 dark:bg-slate-950',
              isMobile ? 'h-[70vh] rounded-t-[1.75rem]' : 'w-[28rem] max-w-[95vw]',
            )}
          >
            <SheetTitle className="sr-only">Workspace 文件</SheetTitle>
            <WorkspacePanel agentId={agentId} capability={workspaceFiles} open={workspacePanelOpen} />
          </SheetContent>
        </Sheet>
      ) : null}

      <style
        dangerouslySetInnerHTML={{
          __html: `
            .custom-scrollbar::-webkit-scrollbar {
              width: 6px;
              height: 6px;
            }
            .custom-scrollbar::-webkit-scrollbar-track {
              background: transparent;
            }
            .custom-scrollbar::-webkit-scrollbar-thumb {
              background-color: rgba(156, 163, 175, 0.3);
              border-radius: 20px;
            }
            .custom-scrollbar:hover::-webkit-scrollbar-thumb {
              background-color: rgba(156, 163, 175, 0.5);
            }
          `,
        }}
      />
    </div>
  );
}
