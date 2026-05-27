/**
 * 这些函数是输入框右下角“上下文状态”的纯计算层。
 * 单独抽出来有两个目的：
 * 1. 前端 UI 只负责展示，不在组件里塞一堆口径判断。
 * 2. 可以直接被 node --test 回归，避免每次都靠人工看页面。
 */

export function coercePositiveNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return null;
}

function isCjkChar(char) {
  return /[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]/u.test(char);
}

export function estimateTextTokens(text) {
  const normalized = String(text || '').trim();
  if (!normalized) {
    return 0;
  }

  let cjkTokens = 0;
  let asciiChars = 0;
  for (const char of normalized) {
    if (isCjkChar(char)) {
      cjkTokens += 1;
      continue;
    }
    asciiChars += 1;
  }
  return Math.max(1, cjkTokens + Math.ceil(asciiChars / 4));
}

export function estimateMessageContextTokens(message) {
  if (!message) {
    return 0;
  }
  // 系统提示通常不再回灌给模型；checkpoint 摘要是例外。
  if (message.role === 'system') {
    return message.eventType === 'context_checkpoint'
      ? estimateTextTokens(message.summary || '')
      : 0;
  }

  let total = estimateTextTokens(message.content || '');
  if (message.tools) {
    total += Object.values(message.tools).reduce((sum, tool) => {
      return sum
        + estimateTextTokens(tool?.name || '')
        + estimateTextTokens(tool?.args || '')
        + estimateTextTokens(tool?.output || '');
    }, 0);
  }
  return total;
}

export function resolveContextWindowTokens(model) {
  if (!model) {
    return null;
  }
  return coercePositiveNumber(model.context_window_tokens)
    ?? coercePositiveNumber(model?.limits?.context_window_tokens);
}

export function resolveAutoCompactThresholdPercent(model) {
  if (!model) {
    return 84;
  }
  return coercePositiveNumber(model.auto_compact_threshold_percentage) ?? 84;
}

export function buildComposerContextIndicator({ messages, draftInput, selectedModel }) {
  const contextWindowTokens = resolveContextWindowTokens(selectedModel);
  if (!contextWindowTokens) {
    return null;
  }

  const activeCompaction = [...(messages || [])].reverse().find((message) => (
    message?.role === 'system'
    && message?.eventType === 'context_checkpoint'
    && message?.status === 'running'
  ));
  if (activeCompaction) {
    const usedTokens = estimateMessageContextTokens(activeCompaction);
    return {
      label: '正在压缩上下文…',
      phase: 'compressing',
      usedTokens,
      contextWindowTokens,
      percent: Math.min(100, Math.max(0, Math.round((usedTokens / contextWindowTokens) * 100))),
    };
  }

  const totalTokens = (messages || []).reduce((sum, message) => {
    return sum + estimateMessageContextTokens(message);
  }, 0) + estimateTextTokens(draftInput || '');
  if (totalTokens <= 0) {
    return null;
  }

  const percent = Math.min(100, Math.max(0, Math.round((totalTokens / contextWindowTokens) * 100)));
  const warningThreshold = resolveAutoCompactThresholdPercent(selectedModel);
  if (percent >= warningThreshold) {
    return {
      label: `估算上下文 ${percent}% · 即将压缩`,
      phase: 'warning',
      percent,
      usedTokens: totalTokens,
      contextWindowTokens,
    };
  }
  return {
    label: `估算上下文 ${percent}%`,
    phase: 'normal',
    percent,
    usedTokens: totalTokens,
    contextWindowTokens,
  };
}
