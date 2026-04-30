function decodeEscapedUnicode(text) {
  return String(text || '').replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) =>
    String.fromCharCode(Number.parseInt(hex, 16)),
  );
}

function parseJsonLikeString(value) {
  let text = String(value || '').trim();
  for (let index = 0; index < 3; index += 1) {
    if (!text) return text;
    const first = text[0];
    const last = text[text.length - 1];
    const looksJson =
      (first === '{' && last === '}') ||
      (first === '[' && last === ']') ||
      (first === '"' && last === '"');
    if (!looksJson) return text.replace(/\\n/g, '\n').replace(/\\"/g, '"');
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed !== 'string') {
        return parseNestedJsonStrings(parsed);
      }
      text = parsed.trim();
    } catch {
      const decoded = decodeEscapedUnicode(text);
      return decoded.replace(/\\n/g, '\n').replace(/\\"/g, '"');
    }
  }
  return text;
}

function parseNestedJsonStrings(value) {
  if (typeof value === 'string') {
    return parseJsonLikeString(value);
  }
  if (Array.isArray(value)) {
    return value.map((item) => parseNestedJsonStrings(item));
  }
  if (!value || typeof value !== 'object') {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, nestedValue]) => [key, parseNestedJsonStrings(nestedValue)]),
  );
}

export function formatToolPayload(value) {
  if (value === undefined || value === null || value === '') {
    return '';
  }
  const parsed = typeof value === 'string' ? parseJsonLikeString(value) : parseNestedJsonStrings(value);
  if (typeof parsed === 'string') {
    return parsed;
  }
  return JSON.stringify(parsed, null, 2);
}
