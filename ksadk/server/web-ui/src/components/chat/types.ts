export type MessageAttachment = {
  name: string;
  url: string;
  type: string;
  fileUri?: string;
};

export type PreviewImageSize = {
  width: number;
  height: number;
};

export type Message = {
  id: string;
  role: 'user' | 'model' | 'tool' | 'system';
  content: string;
  timestamp: number;
  responseId?: string;
  eventId?: string;
  traceId?: string;
  rootSpanId?: string;
  eventType?: string;
  status?: 'running' | 'completed' | 'failed' | 'cancelled';
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
      approvalRequestId?: string;
      previousResponseId?: string;
      serverLabel?: string;
      approvalStatus?: 'pending' | 'approved' | 'rejected';
    };
  };
  attachments?: MessageAttachment[];
  feedback?: {
    agentId?: string;
    sessionId?: string;
    responseId?: string;
    eventId?: string;
    rating: 'up' | 'down';
    comment?: string;
    traceId?: string;
    rootSpanId?: string;
    updatedAt?: string;
    pending?: boolean;
    error?: string;
  };
};

export type Session = {
  SessionId: string;
  Title?: string;
  TitleSource?: string;
  Summary?: string;
  FirstPrompt?: string;
  LastPrompt?: string;
  UpdatedAt?: string | number | null;
  ActiveRunStatus?: string;
  ActiveInvocationId?: string;
  Model?: {
    id?: string;
    display_name?: string;
    [key: string]: unknown;
  } | null;
  ContextUsage?: {
    used_tokens?: number;
    context_window_tokens?: number;
    percent?: number;
    [key: string]: unknown;
  } | null;
};

export type ModelCatalogItem = {
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
    multimodal_input_image?: boolean;
    multimodal_input_video?: boolean;
    multimodal_input_file?: boolean;
  };
  pricing?: Record<string, string | number>;
  [key: string]: unknown;
};

export type ComposerContextIndicator = {
  label: string;
  phase?: 'default' | 'normal' | 'warning' | 'compressing';
  percent?: number;
  usedTokens?: number;
  contextWindowTokens?: number;
} | null;

export type WorkspaceFilesCapability = {
  Enabled: boolean;
  MaxUploadBytes: number;
  SupportsDelete: boolean;
  RootLabel: string;
  EntryAction?: string;
  UploadAction?: string;
  ContentPath?: string;
};

export type WorkspaceEntry = {
  Name: string;
  Path: string;
  Type: 'file' | 'directory';
  SizeBytes?: number | null;
  MimeType?: string | null;
  ModifiedAt?: string | null;
};
