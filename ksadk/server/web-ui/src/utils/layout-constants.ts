import type { RuntimeApiFormat } from '../types/api.js';

const DEFAULT_WORKSPACE_PANEL_WIDTH = 820;
const MIN_WORKSPACE_PANEL_WIDTH = 420;
const MAX_WORKSPACE_PANEL_WIDTH = 1280;
const MIN_CHAT_PANEL_WIDTH = 360;
const DESKTOP_SIDEBAR_WIDTH = 280;

export {
  DEFAULT_WORKSPACE_PANEL_WIDTH,
  MIN_WORKSPACE_PANEL_WIDTH,
  MAX_WORKSPACE_PANEL_WIDTH,
  MIN_CHAT_PANEL_WIDTH,
  DESKTOP_SIDEBAR_WIDTH,
};

export function resolveRunAgentApiFormat(options: {
  agentFramework: string;
  apiFormats: RuntimeApiFormat[];
}): RuntimeApiFormat {
  const { apiFormats } = options;
  if (apiFormats.includes('responses')) {
    return 'responses';
  }
  if (apiFormats.includes('chat_completions')) {
    return 'chat_completions';
  }
  return 'responses';
}

export function clampWorkspacePanelWidth(
  width: number,
  viewportWidth: number,
  sidebarWidth: number,
) {
  const maxWidth = Math.min(
    MAX_WORKSPACE_PANEL_WIDTH,
    Math.max(MIN_WORKSPACE_PANEL_WIDTH, viewportWidth - sidebarWidth - MIN_CHAT_PANEL_WIDTH),
  );
  return Math.min(Math.max(width, MIN_WORKSPACE_PANEL_WIDTH), maxWidth);
}