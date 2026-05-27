const ACTIVE_RUN_STATUSES = new Set(['in_progress', 'running', 'queued', 'pending']);
const DEFAULT_ACTIVE_STALE_AFTER_MS = 5 * 60 * 1000;

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

function formatCompactUpdatedAt(ts) {
  if (!ts) return '';
  const date = typeof ts === 'number'
    ? new Date(ts > 1e11 ? ts : ts * 1000)
    : new Date(ts);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
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
  return isSessionRunningWithOptions(session);
}

function isSessionRunningWithOptions(session, options = {}) {
  if (!ACTIVE_RUN_STATUSES.has(normalizeText(session?.ActiveRunStatus))) {
    return false;
  }
  const updatedAt = sessionUpdatedAtValue(session);
  if (!updatedAt) {
    return true;
  }
  const now = Number.isFinite(Number(options.now)) ? Number(options.now) : Date.now();
  const activeStaleAfterMs = Number.isFinite(Number(options.activeStaleAfterMs))
    ? Number(options.activeStaleAfterMs)
    : DEFAULT_ACTIVE_STALE_AFTER_MS;
  return now - updatedAt <= activeStaleAfterMs;
}

export function normalizeSidebarSessions(sessions = [], query = '', options = {}) {
  const needle = normalizeText(query);
  return (Array.isArray(sessions) ? sessions : [])
    .filter((session) => session?.SessionId)
    .filter((session) => !needle || sessionSearchText(session).includes(needle))
    .slice()
    .sort((left, right) => {
      const leftActive = isSessionRunningWithOptions(left, options) ? 1 : 0;
      const rightActive = isSessionRunningWithOptions(right, options) ? 1 : 0;
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

export function resolveCompactSessionMeta(session, options = {}) {
  const running = isSessionRunningWithOptions(session, options);
  return {
    running,
    label: running ? '' : formatCompactUpdatedAt(session?.UpdatedAt ?? session?.updated_at),
  };
}
