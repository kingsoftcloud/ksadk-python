import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function readSource(relativePath: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../${relativePath}`, import.meta.url)),
    'utf-8',
  );
}

describe('ChatComposer interaction contract', () => {
  it('keeps running controls consolidated in the primary action button', () => {
    const source = readSource('components/chat/ChatComposer.tsx');

    expect(source).not.toContain('停止接收');
    expect(source).not.toContain('取消运行');
    expect(source).not.toContain('终止后台');
    expect(source).toContain("title={isStreaming ? '停止生成' : '发送消息'}");
    expect(source).toContain('isStreaming ? <StopCircle');
  });

  it('does not block image sends based on stale model capability metadata', () => {
    const source = readSource('components/chat/ConnectedComposer.tsx');

    expect(source).not.toContain('multimodal_input_image === false');
    expect(source).not.toContain('不支持图片输入');
    expect(source).toContain('void submitDraft(draftText, draftAttachments)');
  });
});
