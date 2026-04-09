const SESSION_STORAGE_KEY_PREFIX = 'ksadk:webui:selected-session:';

function normalizeAgentId(agentId) {
  const normalized = String(agentId || '').trim();
  return normalized || 'default-agent';
}

function resolveStorage(storage) {
  if (storage && typeof storage.getItem === 'function') {
    return storage;
  }

  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      return window.localStorage;
    }
  } catch (_error) {
    // Access to localStorage can fail in locked-down browsers; ignore and degrade gracefully.
  }

  return null;
}

export function buildSessionStorageKey(agentId) {
  return `${SESSION_STORAGE_KEY_PREFIX}${normalizeAgentId(agentId)}`;
}

export function readPersistedSessionId(agentId, storage) {
  const backend = resolveStorage(storage);
  if (!backend) {
    return null;
  }

  try {
    const value = String(backend.getItem(buildSessionStorageKey(agentId)) || '').trim();
    return value || null;
  } catch (_error) {
    return null;
  }
}

export function writePersistedSessionId(agentId, sessionId, storage) {
  const backend = resolveStorage(storage);
  if (!backend) {
    return;
  }

  const key = buildSessionStorageKey(agentId);
  const normalizedSessionId = String(sessionId || '').trim();

  try {
    if (normalizedSessionId) {
      backend.setItem(key, normalizedSessionId);
      return;
    }
    backend.removeItem(key);
  } catch (_error) {
    // Ignore storage write failures so the chat UI can keep functioning.
  }
}

export function resolveSessionToRestore(sessions, preferredSessionId) {
  if (!Array.isArray(sessions) || sessions.length === 0) {
    return null;
  }

  const normalizedPreferred = String(preferredSessionId || '').trim();
  if (
    normalizedPreferred &&
    sessions.some((session) => String(session?.SessionId || '').trim() === normalizedPreferred)
  ) {
    return normalizedPreferred;
  }

  for (const session of sessions) {
    const sessionId = String(session?.SessionId || '').trim();
    if (sessionId) {
      return sessionId;
    }
  }

  return null;
}
