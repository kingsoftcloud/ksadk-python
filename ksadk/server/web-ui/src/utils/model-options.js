/**
 * @typedef {'auto' | 'enabled' | 'disabled'} ThinkingMode
 */

const THINKING_MODES = new Set(['auto', 'enabled', 'disabled']);

/**
 * @param {unknown} value
 * @returns {ThinkingMode}
 */
export function normalizeThinkingMode(value) {
  const mode = String(value || '').trim().toLowerCase();
  return THINKING_MODES.has(mode) ? /** @type {ThinkingMode} */ (mode) : 'auto';
}

/**
 * @param {unknown} value
 */
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
