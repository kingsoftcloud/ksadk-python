import { describe, expect, it } from 'vitest';
import { buildSandboxedHtml } from '../utils/sandbox.js';

describe('buildSandboxedHtml', () => {
  it('keeps workspace HTML previews network-isolated while allowing local assets', () => {
    const html = buildSandboxedHtml('<html><head></head><body>ok</body></html>', {
      channelId: 'preview-1',
      basePath: '/agentengine/api/v1/ws/agent-1/demo/',
    });

    expect(html).toContain("connect-src 'none'");
    expect(html).toContain('img-src data: blob:');
    expect(html).not.toContain("connect-src 'self'");
    expect(html).not.toContain('img-src data: blob: https:');
    expect(html).toContain('channelId: "preview-1"');
  });

  it('escapes injected meta and base attributes', () => {
    const html = buildSandboxedHtml('<body>ok</body>', {
      basePath: '/agentengine/api/v1/ws/agent-1/a"b/',
    });

    expect(html).toContain('<base href="/agentengine/api/v1/ws/agent-1/a&quot;b/">');
    expect(html).not.toContain('<base href="/agentengine/api/v1/ws/agent-1/a"b/">');
  });
});
