export function textFromUnknown(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && 'text' in item && typeof item.text === 'string') {
          return item.text;
        }
        return '';
      })
      .join('');
  }
  return '';
}

export function objectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

export function extractChatCompletionsStreamDelta(data: unknown): {
  content: string;
  reasoning: string;
  finalText: string;
} {
  const choices = objectRecord(data).choices;
  const firstChoice = objectRecord(Array.isArray(choices) ? choices[0] : null);
  const delta = objectRecord(firstChoice.delta);
  const message = objectRecord(firstChoice.message);
  return {
    content: textFromUnknown(delta.content),
    reasoning: textFromUnknown(delta.reasoning_content),
    finalText: textFromUnknown(message.content),
  };
}