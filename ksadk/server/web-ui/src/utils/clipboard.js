/**
 * 浏览器复制能力在不同运行环境里差异很大：
 * - `navigator.clipboard.writeText` 最理想，但可能因为权限/上下文失败
 * - 本地调试或某些嵌入容器里，只能退回到 `document.execCommand('copy')`
 *
 * 这里统一做一层兜底，让 UI 只关心“最终有没有复制成功”。
 */
export async function copyTextToClipboard(text, env = globalThis) {
  const value = String(text ?? '');
  const navigatorLike = env?.navigator;
  if (navigatorLike?.clipboard?.writeText) {
    try {
      await navigatorLike.clipboard.writeText(value);
      return true;
    } catch {
      // 继续走传统 fallback，不在这里提前失败。
    }
  }

  const documentLike = env?.document;
  const body = documentLike?.body;
  if (!documentLike?.createElement || !body?.appendChild || !body?.removeChild) {
    return false;
  }

  const textarea = documentLike.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute?.('readonly', '');
  if (textarea.style) {
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '-9999px';
    textarea.style.opacity = '0';
    textarea.style.pointerEvents = 'none';
  }

  const activeElement = documentLike.activeElement;
  body.appendChild(textarea);
  textarea.focus?.();
  textarea.select?.();
  textarea.setSelectionRange?.(0, value.length);

  try {
    if (typeof documentLike.execCommand !== 'function') {
      return false;
    }
    return !!documentLike.execCommand('copy');
  } finally {
    body.removeChild(textarea);
    activeElement?.focus?.();
  }
}
