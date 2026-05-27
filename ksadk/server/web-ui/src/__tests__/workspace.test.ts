import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  buildWorkspaceFileBaseUrl,
  buildWorkspaceFileUrl,
  normalizeWorkspacePath,
} from '../utils/workspace.js';

function readSource(relativePath: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../${relativePath}`, import.meta.url)),
    'utf-8',
  );
}

describe('workspace file urls', () => {
  it('builds direct runtime file URLs for hosted data plane previews', () => {
    expect(buildWorkspaceFileUrl('showcase/index.html')).toBe(
      '/_ksadk/workspace/v1/files/showcase/index.html',
    );
    expect(buildWorkspaceFileUrl('show case/a#b.html')).toBe(
      '/_ksadk/workspace/v1/files/show%20case/a%23b.html',
    );
  });

  it('builds directory base URLs for sibling assets without action routes', () => {
    expect(buildWorkspaceFileBaseUrl('showcase/index.html')).toBe(
      '/_ksadk/workspace/v1/files/showcase/',
    );
    expect(buildWorkspaceFileBaseUrl('index.html')).toBe('/_ksadk/workspace/v1/files/');
  });

  it('normalizes delete paths before sending workspace delete actions', () => {
    expect(normalizeWorkspacePath('/showcase/')).toBe('showcase');
    expect(normalizeWorkspacePath('showcase/.')).toBe('showcase');
  });

  it('collapses dot segments and backslashes in workspace paths', () => {
    expect(normalizeWorkspacePath('\\showcase\\')).toBe('showcase');
    expect(normalizeWorkspacePath('showcase/./sub/..')).toBe('showcase');
  });

  it('opens HTML previews from the current sandbox document instead of forcing download URLs', () => {
    const htmlPreview = readSource('components/workspace/HtmlPreview.tsx');

    expect(htmlPreview).toContain("new Blob([previewSrc]");
    expect(htmlPreview).toContain('URL.createObjectURL(blob)');
    expect(htmlPreview).toContain("useState<'editor' | 'preview'>('preview')");
  });
});
