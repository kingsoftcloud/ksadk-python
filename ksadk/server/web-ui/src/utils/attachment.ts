export function fileFingerprint(file: File): string {
  return [file.name, file.size, file.lastModified, file.type].join(':');
}

export function mergeAttachmentFiles(current: File[], incoming: File[]): File[] {
  const merged = new Map<string, File>();
  for (const file of current) {
    merged.set(fileFingerprint(file), file);
  }
  for (const file of incoming) {
    merged.set(fileFingerprint(file), file);
  }
  return Array.from(merged.values());
}

export function extractClipboardFiles(event: React.ClipboardEvent<HTMLTextAreaElement>): File[] {
  return Array.from(event.clipboardData.items || [])
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
}