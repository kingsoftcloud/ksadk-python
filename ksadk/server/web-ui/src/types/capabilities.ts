import type { RuntimeApiFormat } from './api.js';

export type UiCapabilities = {
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