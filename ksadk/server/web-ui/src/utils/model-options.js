const THINKING_MODES = new Set(['auto', 'enabled', 'disabled']);

export function normalizeThinkingMode(value) {
  const mode = String(value || '').trim().toLowerCase();
  return THINKING_MODES.has(mode) ? mode : 'auto';
}

export function buildModelOptionsFromThinkingMode(value) {
  const mode = normalizeThinkingMode(value);
  if (mode === 'disabled') {
    return { thinking: { type: 'disabled' } };
  }
  if (mode === 'enabled') {
    return { thinking: { type: 'enabled' } };
  }
  return undefined;
}
