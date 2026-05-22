import type { RuntimeApiFormat } from './api.js';

export type UiCapabilities = {
  Attachments?: boolean;
  Approval?: boolean;
  Thinking?: boolean;
  StopRun?: boolean;
  ResumeRun?: boolean;
  MCP?: boolean;
  HostedRuntime?: boolean;
  HostedChat: {
    Enabled: boolean;
    ApiFormats: RuntimeApiFormat[];
  };
  NativeDashboard: {
    Enabled: boolean;
    Href?: string | null;
    Label?: string | null;
  };
  NativeTerminal: {
    Enabled: boolean;
    Mode?: string | null;
    Protocol?: string | null;
    Path?: string | null;
  };
  RunLifecycle: {
    Enabled: boolean;
    Resume: boolean;
    Abort: boolean;
  };
  WorkspaceFiles?: boolean;
};
