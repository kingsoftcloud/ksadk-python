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
    };
  };
  attachments?: MessageAttachment[];
};

export type Session = {
  SessionId: string;
  Title?: string;
  TitleSource?: string;
  Summary?: string;
  FirstPrompt?: string;
  LastPrompt?: string;
  UpdatedAt?: string | number | null;
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
  };
  pricing?: Record<string, number>;
  [key: string]: unknown;
};

export type ComposerContextIndicator = {
  label: string;
  phase?: 'default' | 'warning' | 'compressing';
} | null;
