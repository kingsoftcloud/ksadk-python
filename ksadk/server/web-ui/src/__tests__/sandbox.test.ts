import { describe, expect, it } from 'vitest';
import { buildSandboxedHtml } from '../utils/sandbox.js';

describe('buildSandboxedHtml', () => {
  it('keeps workspace HTML previews isolated while allowing CDN render assets', () => {
    const html = buildSandboxedHtml('<html><head></head><body>ok</body></html>', {
      channelId: 'preview-1',
      basePath: '/_ksadk/workspace/v1/files/demo/',
    });

    expect(html).toContain("connect-src 'none'");
    expect(html).toContain("script-src 'unsafe-inline' 'unsafe-eval' 'self' https:");
    expect(html).toContain("style-src 'unsafe-inline' data: 'self' https:");
    expect(html).toContain("img-src data: blob: 'self' https:");
    expect(html).toContain("font-src data: 'self' https:");
    expect(html).not.toContain("connect-src 'self'");
    expect(html).toContain('channelId: "preview-1"');
  });

  it('escapes injected meta and base attributes', () => {
    const html = buildSandboxedHtml('<body>ok</body>', {
      basePath: '/_ksadk/workspace/v1/files/a"b/',
    });

    expect(html).toContain('<base href="/_ksadk/workspace/v1/files/a&quot;b/">');
    expect(html).not.toContain('<base href="/_ksadk/workspace/v1/files/a"b/">');
  });

  it('handles hash-only anchors inside the preview document', () => {
    const html = buildSandboxedHtml('<body><a href="#features">Features</a></body>', {
      channelId: 'preview-1',
      basePath: '/_ksadk/workspace/v1/files/showcase/',
    });

    expect(html).toContain("rawHref.charAt(0) === '#'");
    expect(html).toContain('scrollIntoView');
    expect(html).toContain('event.preventDefault()');
  });
});
