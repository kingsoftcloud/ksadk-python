const ACTIVE_RUN_STATUSES = new Set(['in_progress', 'running', 'queued', 'pending']);

function sessionUpdatedAtValue(session) {
  const raw = session?.UpdatedAt ?? session?.updated_at;
  if (typeof raw === 'string') {
    const parsed = Date.parse(raw);
    return Number.isNaN(parsed) ? 0 : parsed;
  }
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return raw;
  }
  return 0;
}

function normalizeText(value) {
  return String(value || '').trim().toLowerCase();
}

function sessionSearchText(session) {
  return [
    session?.Title,
    session?.Summary,
    session?.FirstPrompt,
    session?.LastPrompt,
    session?.SessionId,
    session?.Model?.display_name,
    session?.Model?.id,
  ].map(normalizeText).join(' ');
}

export function isSessionRunning(session) {
  return ACTIVE_RUN_STATUSES.has(normalizeText(session?.ActiveRunStatus));
}

export function normalizeSidebarSessions(sessions = [], query = '') {
  const needle = normalizeText(query);
  return (Array.isArray(sessions) ? sessions : [])
    .filter((session) => session?.SessionId)
    .filter((session) => !needle || sessionSearchText(session).includes(needle))
    .slice()
    .sort((left, right) => {
      const leftActive = isSessionRunning(left) ? 1 : 0;
      const rightActive = isSessionRunning(right) ? 1 : 0;
      if (leftActive !== rightActive) {
        return rightActive - leftActive;
      }
      return sessionUpdatedAtValue(right) - sessionUpdatedAtValue(left);
    });
}

export function formatSessionModelLabel(session) {
  const model = session?.Model;
  if (!model || typeof model !== 'object') {
    return '';
  }
  return String(model.display_name || model.id || '').trim();
}

export function formatSessionContextLabel(session) {
  const usage = session?.ContextUsage;
  if (!usage || typeof usage !== 'object') {
    return '';
  }
  const percent = Number(usage.percent);
  if (Number.isFinite(percent) && percent >= 0) {
    return `上下文 ${Math.round(percent)}%`;
  }
  const usedTokens = Number(usage.used_tokens ?? usage.usedTokens);
  const windowTokens = Number(usage.context_window_tokens ?? usage.contextWindowTokens);
  if (Number.isFinite(usedTokens) && Number.isFinite(windowTokens) && windowTokens > 0) {
    return `上下文 ${Math.round((usedTokens / windowTokens) * 100)}%`;
  }
  return '';
}
