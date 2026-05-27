import { describe, expect, it } from 'vitest';

import { isChunkLoadError } from '@/components/ErrorBoundary';

describe('ErrorBoundary chunk load recovery', () => {
  it('recognizes stale lazy chunk loading failures', () => {
    expect(isChunkLoadError(new Error('Failed to fetch dynamically imported module'))).toBe(true);
    expect(isChunkLoadError(new Error('Loading chunk CodeBlock failed'))).toBe(true);
    expect(isChunkLoadError(new Error('Importing a module script failed'))).toBe(true);
    expect(isChunkLoadError(new TypeError('error loading dynamically imported module'))).toBe(true);
  });

  it('does not treat normal render errors as chunk load failures', () => {
    expect(isChunkLoadError(new Error('Cannot read properties of undefined'))).toBe(false);
  });
});
