import { describe, expect, it } from 'vitest';
import {
  buildWorkspaceFileBaseUrl,
  buildWorkspaceFileUrl,
  normalizeWorkspacePath,
} from '../utils/workspace.js';

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
});
