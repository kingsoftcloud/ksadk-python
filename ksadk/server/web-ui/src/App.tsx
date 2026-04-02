import React, { useState, useEffect, useRef } from 'react';
import { Send, Plus, Paperclip, PanelLeftClose, PanelLeft, Bot, User, Trash2, StopCircle, RefreshCcw } from 'lucide-react';
import { MessageMarkdown } from './components/MessageMarkdown';
import { Check } from 'lucide-react';
import { buildComposerContextIndicator } from './utils/context.js';

function cn(...classes: (string | undefined | null | false)[]) {
  return classes.filter(Boolean).join(' ');
}

type Message = {
  id: string;
  role: 'user' | 'model' | 'tool' | 'system';
  content: string;
  timestamp: number;
  eventType?: string;
  status?: 'running' | 'completed' | 'failed';
  summary?: string;
  trigger?: string;
  compactedUntilSeqId?: number;
  historical?: boolean;
  reasoning?: string;
  tools?: {
    [name: string]: {
      name: string;
      args: string;
      output?: string;
      status: 'running' | 'completed' | 'error' | 'paused';
    }
  };
  attachments?: {
    name: string;
    url: string;
    type: string;
  }[];
};

type Session = {
  SessionId: string;
  UpdatedAt?: string | number | null;
};

type SessionEventRecord = {
  EventId?: string;
  EventType?: string;
  Content?: {
    role?: string;
    status?: string;
    detail?: string;
    parts?: Array<any>;
  };
  Timestamp?: number;
  Metadata?: Record<string, any>;
  SeqId?: number;
};

type ParsedMessageContent = {
  text: string;
  attachments?: {
    name: string;
    url: string;
    type: string;
  }[];
};

type CompactionStreamPayload = {
  phase?: 'start' | 'done' | 'failed';
  trigger?: 'auto' | 'prompt_too_long';
  compacted_until_seq_id?: number;
  timestamp?: number;
};

type ModelCatalogItem = {
  id: string;
  display_name?: string;
  context_window_tokens?: number;
  max_output_tokens?: number;
  auto_compact_threshold_tokens?: number;
  auto_compact_threshold_percentage?: number;
  limits?: {
    context_window_tokens?: number;
    max_input_tokens?: number;
    max_output_tokens?: number;
    max_reasoning_tokens?: number;
    rpm?: number;
    tpm?: number;
  };
  capabilities?: {
    function_calling?: boolean;
    structured_output?: boolean;
    context_caching?: boolean;
  };
  pricing?: Record<string, number>;
  [key: string]: any;
};

type BootstrapModel = ModelCatalogItem & {
  source?: string;
};

const COMPOSER_MAX_HEIGHT = 160;

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

function parseMessageContent(evt: SessionEventRecord): ParsedMessageContent {
  const parts = evt.Content?.parts || [];
  const textSegments: string[] = [];
  const attachments: ParsedMessageContent['attachments'] = [];

  for (const part of parts) {
    if (part?.type === 'input_text' || part?.text) {
      textSegments.push(part.text || '');
      continue;
    }
    if (part?.type === 'input_file' && part.inlineData) {
      attachments.push({
        name: part.inlineData.displayName || 'attachment',
        url: `data:${part.inlineData.mimeType || 'application/octet-stream'};base64,${part.inlineData.data}`,
        type: part.inlineData.mimeType || 'application/octet-stream',
      });
      continue;
    }
    if (part?.type === 'input_file' && part.fileData) {
      attachments.push({
        name: part.fileData.displayName || 'attachment',
        url: '',
        type: part.fileData.mimeType || 'application/octet-stream',
      });
    }
  }

  // canonical transcript 默认把附件元数据放在 Metadata 里，这里兜底恢复展示。
  const metadataAttachments = Array.isArray(evt.Metadata?.attachments) ? evt.Metadata?.attachments : [];
  for (const attachment of metadataAttachments) {
    attachments.push({
      name: attachment.display_name || 'attachment',
      url: '',
      type: attachment.mime_type || 'application/octet-stream',
    });
  }

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

function buildMessageFromSessionEvent(evt: SessionEventRecord): Message | null {
  const eventType = evt.EventType || '';
  if (eventType === 'run_status') {
    if (evt.Content?.status === 'failed') {
      return {
        id: evt.EventId || String(Date.now() + Math.random()),
        role: 'system',
        content: evt.Content?.detail || '本轮运行失败。',
        eventType,
        status: 'failed',
        timestamp: evt.Timestamp || Date.now(),
      };
    }
    return null;
  }

  if (eventType === 'context_checkpoint') {
    const parsed = parseMessageContent(evt);
    return buildCompactionMessage({
      id: evt.EventId || String(Date.now() + Math.random()),
      timestamp: evt.Timestamp || Date.now(),
      status: 'completed',
      trigger: String(evt.Metadata?.trigger || 'auto'),
      compactedUntilSeqId: Number(evt.Metadata?.compacted_until_seq_id || 0) || undefined,
      summary: parsed.text || undefined,
      historical: true,
    });
  }

  if (eventType !== 'user_message' && eventType !== 'assistant_message') {
    return null;
  }

  const parsed = parseMessageContent(evt);
  if (!parsed.text && !parsed.attachments?.length) {
    return null;
  }

  return {
    id: evt.EventId || String(Date.now() + Math.random()),
    role: eventType === 'user_message' ? 'user' : 'model',
    content: parsed.text,
    timestamp: evt.Timestamp || Date.now(),
    eventType,
    attachments: parsed.attachments,
  };
}

export default function App() {
  const [agentId, setAgentId] = useState('default-agent');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<File[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [agentName, setAgentName] = useState('AgentEngine');
  const [selectedModel, setSelectedModel] = useState('');
  const [availableModels, setAvailableModels] = useState<ModelCatalogItem[]>([]);
  const [modelSource, setModelSource] = useState('');
  const [modelCatalogLoaded, setModelCatalogLoaded] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const activeCompactionMessageIdRef = useRef<string | null>(null);

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isStreaming]);

  // Initial load
  useEffect(() => {
    fetchBootstrap();
  }, []);

  useEffect(() => {
    if (!textareaRef.current) {
      return;
    }
    textareaRef.current.style.height = 'auto';
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, COMPOSER_MAX_HEIGHT)}px`;
  }, [input]);

  const fetchModels = async (targetAgentId: string) => {
    try {
      const res = await fetch('/agentengine/api/v1/ListAgentModels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ AgentId: targetAgentId }),
      });
      const data = await res.json();
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
    } catch (e) {
      console.error('Failed to fetch models:', e);
    } finally {
      setModelCatalogLoaded(true);
    }
  };

  const fetchBootstrap = async () => {
    try {
      const res = await fetch('/agentengine/api/v1/GetAgentUiBootstrap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      const bootstrapAgentId = data?.Data?.Agent?.AgentId || 'default-agent';
      setAgentId(bootstrapAgentId);
      if (data?.Data?.Agent?.Name) {
        setAgentName(data.Data.Agent.Name);
        fetchSessions(bootstrapAgentId);
      } else {
        fetchSessions(bootstrapAgentId);
      }
      const bootstrapModel: BootstrapModel | undefined = data?.Data?.Model;
      if (bootstrapModel?.id) {
        setSelectedModel(bootstrapModel.id);
        setAvailableModels((current) => upsertModelOptions(current, [bootstrapModel]));
        setModelSource(bootstrapModel.source || '');
      }
      fetchModels(bootstrapAgentId);
    } catch (e) {
      console.error('Failed to fetch bootstrap:', e);
      fetchSessions('default-agent');
      fetchModels('default-agent');
    }
  };

  const fetchSessions = async (agentId = 'default-agent') => {
    try {
      const res = await fetch('/agentengine/api/v1/ListSessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ AgentId: agentId }),
      });
      const data = await res.json();
      if (data?.Data?.Sessions) {
        const sorted = upsertSessions([], data.Data.Sessions as Session[]);
        setSessions(sorted);
        if (sorted.length > 0 && !currentSessionId) {
          loadSession(sorted[0].SessionId);
        }
      }
    } catch (e) {
      console.error('Failed to fetch sessions:', e);
    }
  };

  const loadSession = async (sessionId: string) => {
    setCurrentSessionId(sessionId);
    activeCompactionMessageIdRef.current = null;
    try {
      const res = await fetch('/agentengine/api/v1/ListSessionEvents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ SessionId: sessionId }),
      });
      const data = await res.json();
      if (data?.Data?.Events) {
        const history = data.Data.Events
          .map((evt: SessionEventRecord) => buildMessageFromSessionEvent(evt))
          .filter((evt: Message | null): evt is Message => Boolean(evt));
        setMessages(history);
      } else {
        setMessages([]);
      }
    } catch (e) {
      console.error('Failed to load session events:', e);
    }
  };

  const createNewSession = async () => {
    if (isStreaming) return;
    try {
      const res = await fetch('/agentengine/api/v1/CreateSession', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ AgentId: agentId }),
      });
      const data = await res.json();
      const newId = data?.Data?.Session?.SessionId;
      if (newId) {
        setSessions(prev => upsertSessions(prev, [{ SessionId: newId, UpdatedAt: new Date().toISOString() }]));
        fetchSessions(agentId);
        setCurrentSessionId(newId);
        setMessages([]);
      }
    } catch (e) {
      console.error('Failed to create session:', e);
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
      payload.phase === 'start' ? 'running' : payload.phase === 'failed' ? 'failed' : 'completed';
    const nextMessage = buildCompactionMessage({
      id: currentId,
      timestamp: payload.timestamp || Date.now(),
      status: nextStatus,
      trigger: payload.trigger,
      compactedUntilSeqId: payload.compacted_until_seq_id,
    });

    setMessages(prev => {
      const existingIndex = prev.findIndex(msg => msg.id === currentId);
      if (existingIndex < 0) {
        return [...prev, nextMessage];
      }
      return prev.map(msg => msg.id === currentId ? { ...msg, ...nextMessage } : msg);
    });

    if (nextStatus !== 'running') {
      activeCompactionMessageIdRef.current = null;
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if ((!input.trim() && attachments.length === 0) || isStreaming) return;
    
    // Fallback ID if none
    let sId = currentSessionId;
    if (!sId) {
      try {
        const sessionResponse = await fetch('/agentengine/api/v1/CreateSession', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ AgentId: agentId }),
        });
        const sessionPayload = await sessionResponse.json();
        sId = sessionPayload?.Data?.Session?.SessionId || null;
      } catch (err) {
        console.error('Failed to create session before RunAgent:', err);
      }
    }
    if (!sId) {
      sId = 'default-session-' + Date.now();
    }
    setSessions(prev => upsertSessions(prev, [{ SessionId: sId, UpdatedAt: new Date().toISOString() }]));
    setCurrentSessionId(sId);

    const userText = input.trim();
    setInput('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
    
    const msgAttachments = attachments.map(f => ({
      name: f.name,
      url: URL.createObjectURL(f),
      type: f.type || 'application/octet-stream'
    }));

    const userMsg: Message = {
      id: String(Date.now()),
      role: 'user',
      content: userText,
      timestamp: Date.now(),
      attachments: msgAttachments.length > 0 ? msgAttachments : undefined
    };
    
    setMessages(prev => [...prev, userMsg]);
    setIsStreaming(true);

    const parts: any[] = [{ type: 'input_text', text: userText }];
    
    // Process attachments via UploadFile API
    for (const file of attachments) {
      if (file.size > 100 * 1024 * 1024) {
        setMessages(prev => [...prev, {
            id: String(Date.now()),
            role: 'model',
            content: `【系统提示】文件 ${file.name} 超过 100MB 限制，未发送。`,
            timestamp: Date.now()
        }]);
        continue;
      }

      const formData = new FormData();
      formData.append("file", file);
      try {
          const uploadRes = await fetch('/agentengine/api/v1/UploadFile', {
              method: 'POST',
              body: formData
          });
          if (!uploadRes.ok) {
              const errorText = await uploadRes.text();
              throw new Error(`HTTP ${uploadRes.status}: ${errorText}`);
          }
          const uploadData = await uploadRes.json();
          if (uploadData?.Data?.FileData) {
              parts.push({
                  type: 'input_file',
                  fileData: {
                      fileUri: uploadData.Data.FileData.fileUri,
                      displayName: uploadData.Data.FileData.displayName || file.name,
                      mimeType: uploadData.Data.FileData.mimeType || file.type || "application/octet-stream"
                  }
              });
          } else if (uploadData?.Message !== "Success") {
              throw new Error(`服务端返回异常: ${uploadData?.Message || JSON.stringify(uploadData)}`);
          }
      } catch (err: any) {
          console.error("Upload failed", err);
          setMessages(prev => [...prev, {
              id: String(Date.now()),
              role: 'model',
              content: `【系统提示】文件 ${file.name} 上传失败，原因: ${err.message}`,
              timestamp: Date.now()
          }]);
      }
    }

    setAttachments([]); // Clear attachments after sending

    abortControllerRef.current = new AbortController();

    try {
      const response = await fetch('/agentengine/api/v1/RunAgent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({
          AgentId: agentId,
          SessionId: sId,
          Stream: true,
          ApiFormat: 'responses',
          Model: selectedModel || undefined,
          Messages: [
            {
              role: 'user',
              content: parts,
            },
          ],
        }),
        signal: abortControllerRef.current.signal
      });

      if (!response.body) throw new Error('No readable stream');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      
      let assistantMsgId = String(Date.now() + 1);
      let assistantMessageCreated = false;
      const ensureAssistantMessage = () => {
        if (assistantMessageCreated) return;
        assistantMessageCreated = true;
        setMessages(prev => [
          ...prev,
          { id: assistantMsgId, role: 'model', content: '', timestamp: Date.now(), reasoning: '' },
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

        // 按 SSE block 解析，避免上一条 event 名称泄漏到下一条消息。
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

          const dataStr = dataLines.join('\n').trim();
          if (dataStr === '[DONE]') {
            isDone = true;
            break;
          }
          if (!dataStr) continue;

          try {
            const data = JSON.parse(dataStr);
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

            // Handle ADK event format or newer SSE formats
            if (currentEvent === 'response.tool_call') {
               ensureAssistantMessage();
               setMessages(prev => prev.map(m => {
                 if (m.id === assistantMsgId) {
                   const name = String(data.name || "tool");
                   const args = typeof data.args === 'object' ? JSON.stringify(data.args, null, 2) : String(data.args || "");
                   return {
                     ...m,
                     tools: {
                       ...(m.tools || {}),
                       [name]: { name, args, status: 'running' }
                     }
                   };
                 }
                 return m;
               }));
            } else if (currentEvent === 'response.tool_result') {
               ensureAssistantMessage();
               setMessages(prev => prev.map(m => {
                 if (m.id === assistantMsgId) {
                   const name = String(data.name || "tool");
                   const output = typeof data.output === 'object' ? JSON.stringify(data.output, null, 2) : String(data.output || "");
                   return {
                     ...m,
                     tools: {
                       ...(m.tools || {}),
                       [name]: { ...(m.tools?.[name] || { name, args: '' }), output, status: 'completed' }
                     }
                   };
                 }
                 return m;
               }));
            } else if (currentEvent === 'response.reasoning.delta') {
              const delta = data.delta || '';
              if (delta) {
                ensureAssistantMessage();
                setMessages(prev => prev.map(m => m.id === assistantMsgId ? { ...m, reasoning: (m.reasoning || '') + delta } : m));
              }
            } else if (currentEvent === 'response.output_text.delta') {
              const delta = data.delta || '';
              if (delta) {
                ensureAssistantMessage();
                setMessages(prev => prev.map(m => m.id === assistantMsgId ? { ...m, content: m.content + delta } : m));
              }
            } else if (currentEvent === 'response.completed') {
              const finalText = data.output_text || '';
              if (finalText) {
                ensureAssistantMessage();
                setMessages(prev => prev.map(m => m.id === assistantMsgId ? { ...m, content: finalText } : m));
              }
            } else if (data.content?.parts?.[0]?.text) {
              // Legacy ADK style
              const delta = data.content.parts[0].text;
              if (!data.actions?.finishReason) {
                 ensureAssistantMessage();
                 setMessages(prev => prev.map(m => m.id === assistantMsgId ? { ...m, content: m.content + delta } : m));
              }
            }
          } catch (err) {
            console.warn("Failed to parse SSE data", dataStr, err);
          }
        }
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
         if (activeCompactionMessageIdRef.current) {
           upsertCompactionMessage({ phase: 'failed' });
         }
         console.log('Stream aborted');
      } else {
         if (activeCompactionMessageIdRef.current) {
           upsertCompactionMessage({ phase: 'failed' });
         }
         console.error('SSE Error:', err);
         setMessages(prev => [...prev, { id: String(Date.now()), role: 'model', content: '连接断开或生成出错。', timestamp: Date.now() }]);
      }
    } finally {
      setIsStreaming(false);
      abortControllerRef.current = null;
      activeCompactionMessageIdRef.current = null;
      fetchSessions(agentId);
    }
  };

  const deleteSession = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await fetch('/agentengine/api/v1/DeleteSession', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ SessionId: id }),
      });
      setSessions(prev => prev.filter(s => s.SessionId !== id));
      if (currentSessionId === id) {
         setMessages([]);
         setCurrentSessionId(null);
      }
    } catch (e) {
      console.error('Failed to delete session', e);
    }
  };

  const formatDate = (ts?: string | number | null) => {
    if (!ts) return '';
    if (typeof ts === 'string') {
      const parsed = Date.parse(ts);
      return Number.isNaN(parsed)
        ? ''
        : new Date(parsed).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }
    const date = new Date(ts > 1e11 ? ts : ts * 1000); // handle ms/s mix
    return date.toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  };

  const selectedModelMetadata = availableModels.find((model) => model.id === selectedModel) || null;
  const selectedModelLabel = selectedModelMetadata?.display_name || selectedModel || '';
  const composerContextIndicator = buildComposerContextIndicator({
    messages,
    draftInput: input,
    selectedModel: selectedModelMetadata,
  });

  return (
    <div className="flex h-screen bg-white text-slate-800 font-sans overflow-hidden dark:bg-slate-900 dark:text-slate-200">
      
      {/* Sidebar - Open WebUI style (minimal dark/light gray) */}
      <aside className={cn(
        "flex-shrink-0 flex flex-col transition-all duration-300 ease-in-out border-r border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950/50",
        sidebarOpen ? "w-[260px]" : "w-0 overflow-hidden border-none"
      )}>
        <div className="p-4 flex flex-col gap-2">
          <button 
            onClick={createNewSession}
            disabled={isStreaming}
            className="flex items-center justify-between w-full px-3 py-2.5 rounded-lg hover:bg-slate-200/50 dark:hover:bg-slate-800 transition-colors text-sm font-medium disabled:opacity-50"
          >
            <div className="flex items-center gap-2">
              <Plus className="w-4 h-4" />
              <span>新对话</span>
            </div>
            <span className="text-xs bg-white dark:bg-slate-800 px-1.5 py-0.5 rounded border border-slate-200 dark:border-slate-700 shadow-sm text-slate-500">⌘ N</span>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-4 flex flex-col gap-1 custom-scrollbar">
          <div className="px-2 py-2 text-xs font-semibold text-slate-400 dark:text-slate-500 mt-2">历史记录</div>
          {sessions.map((session) => (
             <div
               key={session.SessionId}
               onClick={() => !isStreaming && loadSession(session.SessionId)}
               className={cn(
                 "group flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer transition-colors text-sm",
                 currentSessionId === session.SessionId 
                   ? "bg-slate-200 dark:bg-slate-800 text-slate-900 dark:text-slate-100 font-medium" 
                   : "hover:bg-slate-100 dark:hover:bg-slate-800/50 text-slate-600 dark:text-slate-400"
               )}
             >
               <div className="flex-1 min-w-0 pr-2">
                 <div className="truncate">{session.SessionId.slice(0, 12)}...</div>
                 <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-0.5">{formatDate(session.UpdatedAt)}</div>
               </div>
               <button 
                 onClick={(e) => deleteSession(session.SessionId, e)}
                 className="opacity-0 group-hover:opacity-100 p-1.5 text-slate-400 hover:text-red-500 transition-opacity"
                 title="Delete Chat"
               >
                 <Trash2 className="w-3.5 h-3.5" />
               </button>
             </div>
          ))}
        </div>

        {/* Brand Footer */}
        <div className="flex-shrink-0 p-3 pt-4 border-t border-slate-200 dark:border-slate-800 flex flex-col items-center justify-center opacity-80">
          <div className="text-[10px] text-slate-400 dark:text-slate-500 font-medium mb-0.5">POWERED BY</div>
          <div className="font-bold text-xs text-transparent bg-clip-text bg-gradient-to-r from-blue-600 to-indigo-500 dark:from-blue-400 dark:to-indigo-300">
            Ksyun AgentEngine
          </div>
        </div>
      </aside>

      {/* Main Container */}
      <main className="flex-1 flex flex-col min-w-0 bg-white dark:bg-slate-900 relative h-full">
        {/* Top Header */}
        <header className="flex-shrink-0 h-14 px-4 flex items-center justify-between border-b border-transparent">
          <div className="flex items-center gap-2">
            <button 
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="p-2 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-md transition-colors"
            >
              {sidebarOpen ? <PanelLeftClose className="w-5 h-5" /> : <PanelLeft className="w-5 h-5" />}
            </button>
            <div className="font-semibold text-base py-1 px-2 rounded-md hover:bg-slate-100 dark:hover:bg-slate-800 cursor-pointer">
              {agentName} <span className="text-slate-400 font-normal text-sm ml-1">智能体</span>
            </div>
          </div>
          <div className="flex items-center gap-3">
             {availableModels.length > 1 ? (
               <select 
                 value={selectedModel}
                 onChange={(e) => {
                   setSelectedModel(e.target.value);
                 }}
                 className="text-xs bg-slate-50 border border-slate-200 dark:bg-slate-800 dark:border-slate-700 text-slate-700 dark:text-slate-300 rounded px-2 py-1 outline-none focus:ring-1 focus:ring-blue-500 transition-colors max-w-[150px] truncate"
               >
                 {availableModels.map(m => (
                   <option key={m.id} value={m.id}>{m.display_name || m.id}</option>
                 ))}
               </select>
             ) : selectedModelLabel ? (
               <span
                 className="text-xs bg-slate-50 border border-slate-200 dark:bg-slate-800 dark:border-slate-700 text-slate-700 dark:text-slate-300 rounded px-2 py-1 max-w-[180px] truncate"
                 title={modelSource || selectedModelLabel}
               >
                 {selectedModelLabel}
               </span>
             ) : (
               <span className="text-xs text-slate-400">{modelCatalogLoaded ? '未配置模型' : 'Loading models...'}</span>
             )}
             {currentSessionId && (
               <span className="text-xs text-slate-400 font-mono bg-slate-50 dark:bg-slate-800 px-2 py-1 rounded">
                 ID: {currentSessionId.slice(0, 8)}
               </span>
             )}
          </div>
        </header>

        {/* Output Scroll Area */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-5 scroll-smooth">
          <div className="max-w-[64rem] mx-auto flex flex-col pb-8">
            {messages.length === 0 ? (
              <div className="flex flex-col items-center justify-center min-h-[50vh] text-center px-4">
                <div className="w-16 h-16 bg-slate-100 dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
                  <Bot className="w-8 h-8 text-slate-600 dark:text-slate-300" />
                </div>
                <h2 className="text-2xl font-semibold mb-2">有什么我可以帮您的吗？</h2>
                <p className="text-slate-500 dark:text-slate-400 text-sm max-w-md mx-auto">
                  我是 {agentName}，一个由 <span className="font-semibold text-transparent bg-clip-text bg-gradient-to-r from-blue-600 to-indigo-500 dark:from-blue-400 dark:to-indigo-300">Ksyun AgentEngine</span> 驱动的智能体。您可以在下方输入消息开始对话。
                </p>
                <div className="mt-8 grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-2xl">
                   <button onClick={() => setInput('你好，请介绍一下你自己')} className="p-3 text-left border border-slate-200 dark:border-slate-800 rounded-xl hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
                     <div className="text-sm font-medium">✨ 你好，请介绍一下你自己</div>
                   </button>
                   <button onClick={() => setInput('你能做什么？有什么特色技能？')} className="p-3 text-left border border-slate-200 dark:border-slate-800 rounded-xl hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
                     <div className="text-sm font-medium">💡 工具探究</div>
                     <div className="text-xs text-slate-500 mt-1">你能做什么？有什么特色技能？</div>
                   </button>
                </div>
              </div>
	            ) : (
	              messages.map((msg, idx) => (
	                msg.role === 'system' ? (
	                  <div key={msg.id || idx} className="px-4 py-2 w-full">
	                    <div className="mx-auto max-w-3xl rounded-2xl border border-amber-200/80 bg-amber-50/80 px-4 py-3 text-sm text-amber-900 shadow-sm dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
	                      <div className="flex items-center gap-2 font-medium">
	                        {msg.status === 'running' ? (
	                          <RefreshCcw className="h-4 w-4 animate-spin text-amber-600 dark:text-amber-300" />
	                        ) : msg.status === 'failed' ? (
	                          <StopCircle className="h-4 w-4 text-rose-600 dark:text-rose-400" />
	                        ) : (
	                          <Check className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
	                        )}
	                        <span>{msg.content}</span>
	                      </div>
	                      {msg.compactedUntilSeqId && (
	                        <div className="mt-1 text-xs text-amber-700/80 dark:text-amber-200/80">
	                          已折叠到会话事件 #{msg.compactedUntilSeqId}
	                        </div>
	                      )}
	                      {msg.summary && (
	                        <details className="mt-3 rounded-xl border border-amber-200/80 bg-white/70 px-3 py-2 dark:border-amber-900/60 dark:bg-slate-950/40">
	                          <summary className="cursor-pointer select-none text-xs font-medium text-amber-800 dark:text-amber-200">
	                            查看压缩摘要
	                          </summary>
	                          <div className="mt-2 text-[13px] leading-relaxed text-slate-700 dark:text-slate-200">
	                            <MessageMarkdown content={msg.summary} />
	                          </div>
	                        </details>
	                      )}
	                    </div>
	                  </div>
	                ) : (
	                <div key={msg.id || idx} className={cn(
	                  "px-4 py-4 flex gap-4 w-full group",
	                  msg.role === 'user' ? "" : ""
	                )}>
                  {/* Avatar */}
                  <div className="flex-shrink-0 mt-0.5">
                    {msg.role === 'user' ? (
                      <div className="w-7 h-7 bg-slate-800 dark:bg-slate-200 rounded-full flex items-center justify-center text-white dark:text-slate-900">
                        <User className="w-4 h-4" />
                      </div>
                    ) : (
                      <div className="w-7 h-7 bg-emerald-600 rounded-full flex items-center justify-center text-white p-1">
                        <Bot className="w-4 h-4" />
                      </div>
                    )}
                  </div>
                  
                  {/* Content Container */}
                  <div className="flex-1 min-w-0 w-full overflow-hidden">
	                    <div className="font-semibold text-sm mb-1 text-slate-800 dark:text-slate-200">
	                      {msg.role === 'user' ? 'You' : agentName}
                    </div>

                    {/* Attachments rendering */}
                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className="flex flex-wrap gap-3 mb-3">
                        {msg.attachments.map((att, attIdx) => (
                           att.type.startsWith('image/') ? (
                             <img key={attIdx} src={att.url || '#'} alt={att.name} className="max-w-[200px] max-h-[200px] object-cover rounded-lg border border-slate-200 dark:border-slate-700 shadow-sm" />
                           ) : (
                             <div key={attIdx} className="flex items-center gap-2 px-3 py-2 bg-slate-100 dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 w-max shadow-sm">
                               <Paperclip className="w-4 h-4 text-blue-500" />
                               <span className="text-sm text-slate-700 dark:text-slate-300 truncate max-w-[150px]" title={att.name}>{att.name}</span>
                             </div>
                           )
                        ))}
                      </div>
                    )}
                    
                    {/* Reasoning Block */}
                    {msg.reasoning && (
                       <details className="group/details mb-4 rounded-xl border border-slate-200 dark:border-slate-700/50 bg-slate-50/50 dark:bg-slate-800/20 px-4 py-3 text-sm text-slate-600 dark:text-slate-400 marker:content-[''] transition-all">
                         <summary className="flex cursor-pointer select-none items-center gap-2 font-medium list-none justify-between">
                            <div className="flex items-center gap-2">
                               {isStreaming && idx === messages.length - 1 && !msg.content ? (
                                   <RefreshCcw className="h-4 w-4 animate-spin text-emerald-500" />
                               ) : (
                                   <Check className="h-4 w-4 text-emerald-500" />
                               )}
                               <span>思考过程</span>
                            </div>
                         </summary>
                         <div className="mt-3 border-l-2 border-slate-200 dark:border-slate-700 pl-4 py-1 text-[14px] leading-relaxed opacity-90 mx-1">
                            <MessageMarkdown content={msg.reasoning} />
                         </div>
                       </details>
                    )}

                    {/* Tools Block */}
                    {msg.tools && Object.values(msg.tools).map((tool, tIdx) => (
                       <details key={tIdx} className="group/details mb-4 rounded-xl border border-blue-200 dark:border-blue-900/50 bg-blue-50/30 dark:bg-blue-950/20 px-4 py-3 text-sm text-blue-600 dark:text-blue-400 marker:content-[''] transition-all">
                         <summary className="flex cursor-pointer select-none items-center gap-2 font-medium list-none justify-between">
                            <div className="flex items-center gap-2">
                               {tool.status === 'running' ? (
                                   <RefreshCcw className="h-4 w-4 animate-spin text-blue-500" />
                               ) : (
                                   <Check className="h-4 w-4 text-emerald-500" />
                               )}
                               <span>工具调用：{tool.name}</span>
                            </div>
                         </summary>
                         <div className="mt-3 border-l-2 border-blue-200 dark:border-blue-800 pl-4 py-1 text-[13px] font-mono leading-relaxed opacity-90 mx-1 flex flex-col gap-3">
                            {tool.args && (
                              <div>
                                <div className="text-xs font-semibold uppercase text-blue-500 mb-1">入参 (Args)</div>
                                <div className="text-slate-600 dark:text-slate-300 whitespace-pre-wrap">{tool.args}</div>
                              </div>
                            )}
                            {tool.output && (
                              <div>
                                <div className="text-xs font-semibold uppercase text-emerald-500 mb-1">输出 (Output)</div>
                                <div className="text-slate-600 dark:text-slate-300 whitespace-pre-wrap max-h-[300px] overflow-y-auto custom-scrollbar">{tool.output}</div>
                              </div>
                            )}
                         </div>
                       </details>
                    ))}

                    {/* Text Content */}
                    <div className="w-full">
                      {msg.content ? (
                        <MessageMarkdown content={msg.content} />
                      ) : (
                         isStreaming && idx === messages.length - 1 && !msg.reasoning && !msg.tools && (
                           <span className="inline-block w-2 h-4 bg-emerald-500 animate-pulse align-middle ml-1 rounded-sm shadow-sm opacity-80 mt-2"></span>
                         )
                      )}
                    </div>
                  </div>
	                </div>
	                )
	              ))
	            )}
          </div>
        </div>

        {/* Input Area */}
        <div className="relative z-10 w-full flex-shrink-0 bg-gradient-to-t from-white via-white to-transparent px-4 pb-4 pt-2 dark:from-slate-900 dark:via-slate-900">
          <div className="max-w-[64rem] mx-auto relative">
            {isStreaming && (
               <div className="absolute -top-10 left-1/2 z-20 flex -translate-x-1/2 justify-center">
                 <button 
                   onClick={stopGeneration}
                   className="flex items-center gap-2 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-full px-4 py-1.5 shadow-sm text-sm hover:bg-slate-50 dark:hover:bg-slate-700 transition"
                 >
                   <StopCircle className="w-4 h-4 text-slate-500" />
                   <span>停止生成</span>
                 </button>
               </div>
            )}
            <form
              onSubmit={handleSubmit}
              onDragOver={(e) => {
                e.preventDefault();
                e.stopPropagation();
              }}
              onDrop={(e) => {
                e.preventDefault();
                e.stopPropagation();
                if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                  setAttachments(prev => [...prev, ...Array.from(e.dataTransfer.files)]);
                }
              }}
              className={cn(
                "relative flex w-full flex-col rounded-2xl border bg-white shadow-sm transition-all focus-within:border-slate-300 focus-within:ring-1 focus-within:ring-slate-300 dark:bg-slate-900 dark:focus-within:border-slate-600 dark:focus-within:ring-slate-600",
                "border-slate-200 p-1.5 dark:border-slate-700"
              )}
            >
              {attachments.length > 0 && (
                <div className="mb-1.5 flex flex-wrap gap-2 px-1.5 pt-1.5">
                  {attachments.map((file, i) => (
                    <div key={i} className="flex items-center gap-2 bg-slate-100 dark:bg-slate-800 text-xs text-slate-600 dark:text-slate-300 px-3 py-1.5 rounded-xl border border-slate-200 dark:border-slate-700">
                      <span className="truncate max-w-[120px] font-medium">{file.name}</span>
                      <button type="button" onClick={() => setAttachments(prev => prev.filter((_, idx) => idx !== i))} className="hover:text-red-500 text-slate-400">×</button>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex items-end w-full">
                <label 
                  className="relative ml-0.5 flex h-9 w-9 flex-shrink-0 items-center justify-center overflow-hidden rounded-xl transition hover:bg-slate-100 dark:hover:bg-slate-800"
                  title="上传附件"
                >
                  <input
                    type="file"
                    multiple
                    className="hidden"
                    onChange={(e) => {
                      if (e.target.files && e.target.files.length > 0) {
                        const newFiles = Array.from(e.target.files);
                        setAttachments(prev => [...prev, ...newFiles]);
                        e.target.value = "";
                      }
                    }}
                  />
                  <Paperclip className="w-5 h-5 text-slate-400" />
                </label>
                
                <textarea
                  ref={textareaRef}
                  rows={1}
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value);
                    e.target.style.height = 'auto';
                    e.target.style.height = `${Math.min(e.target.scrollHeight, COMPOSER_MAX_HEIGHT)}px`;
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleSubmit(e);
                    }
                  }}
                  placeholder="发送消息... (Shift + Enter 换行)"
                  className="custom-scrollbar min-h-[36px] w-full max-h-[160px] resize-none border-0 bg-transparent px-2 py-2 text-[15px] leading-6 outline-none placeholder:text-slate-400 dark:text-slate-100 dark:placeholder:text-slate-500"
                  style={{ overflowY: 'auto' }}
                />
                
                <button
                  type="submit"
                  disabled={!input.trim() && attachments.length === 0 || isStreaming}
                  className={cn(
                    "mb-0.5 ml-1 flex-shrink-0 rounded-xl p-2 transition-all",
                    (input.trim() || attachments.length > 0) && !isStreaming
                      ? "bg-slate-900 text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                      : "bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-600"
                  )}
                >
                   <Send className="w-4 h-4 ml-0.5" />
                </button>
              </div>
            </form>
            <div className="mt-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-1">
              <div className="text-[11px] text-slate-400 dark:text-slate-500">
                Agent 可能产生不准确的信息，请独立验证。
              </div>
              {composerContextIndicator && (
                <div
                  className={cn(
                    "ml-auto text-[11px] leading-5 text-right transition-colors",
                    composerContextIndicator.phase === 'compressing'
                      ? "text-amber-500 dark:text-amber-300"
                      : composerContextIndicator.phase === 'warning'
                        ? "text-rose-500 dark:text-rose-300"
                        : "text-slate-400 dark:text-slate-500"
                  )}
                >
                  {composerContextIndicator.label}
                </div>
              )}
            </div>
          </div>
        </div>

      </main>

      <style dangerouslySetInnerHTML={{__html: `
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
      `}} />
    </div>
  );
}
