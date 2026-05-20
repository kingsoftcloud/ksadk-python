import { useEffect, useRef } from 'react';

type SandboxOptions = {
  channelId?: string;
  basePath?: string;
};

function escapeHtmlAttribute(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function cspSourceForBasePath(basePath: string): string {
  const normalizedBasePath = basePath.endsWith('/') ? basePath : `${basePath}/`;
  if (typeof window === 'undefined') {
    return "'self'";
  }
  try {
    return new URL(normalizedBasePath, window.location.origin).toString();
  } catch {
    return "'self'";
  }
}

function buildPreviewCsp(basePath?: string): string {
  if (basePath) {
    const assetSource = cspSourceForBasePath(basePath);
    return [
      "default-src 'none'",
      `script-src 'unsafe-inline' 'unsafe-eval' ${assetSource}`,
      `style-src 'unsafe-inline' data: ${assetSource}`,
      `img-src data: blob: ${assetSource}`,
      `font-src data: ${assetSource}`,
      `media-src data: blob: ${assetSource}`,
      "worker-src blob:",
      "connect-src 'none'",
      "form-action 'none'",
      "base-uri 'self'",
    ].join('; ');
  }

  return [
    "default-src 'none'",
    "script-src 'unsafe-inline' 'unsafe-eval'",
    "style-src 'unsafe-inline' data:",
    "img-src data: blob:",
    "font-src data:",
    "media-src data: blob:",
    "worker-src blob:",
    "connect-src 'none'",
    "form-action 'none'",
  ].join('; ');
}

function isSafeLinkHref(href: string): boolean {
  try {
    const url = new URL(href, window.location.href);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol);
  } catch {
    return false;
  }
}

/**
 * Build a sandboxed HTML document for iframe srcdoc.
 * Injects CSP and link click interception.
 * When basePath is provided, sibling workspace assets may load through the
 * workspace route, while XHR/fetch/websocket connections stay disabled.
 */
export function buildSandboxedHtml(html: string, options: SandboxOptions | string = {}): string {
  const opts = typeof options === 'string' ? { channelId: options } : options;
  const { channelId, basePath } = opts;

  const csp = buildPreviewCsp(basePath);

  const interceptor = `<script>
document.addEventListener('click', function(e) {
  var target = e.target.closest('a');
  if (target && target.href) {
    e.preventDefault();
    window.parent.postMessage({
      type: 'ksadk:linkClick',
      channelId: ${JSON.stringify(channelId || '')},
      href: target.href,
      target: target.target || '_self'
    }, '*');
  }
}, true);
</script>`;

  const cspMeta = `<meta http-equiv="Content-Security-Policy" content="${escapeHtmlAttribute(csp)}">`;
  const baseTag = basePath ? `<base href="${escapeHtmlAttribute(basePath)}">` : '';

  const inject = `${cspMeta}${baseTag}${interceptor}`;

  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head[^>]*>/i, `$&${inject}`);
  }
  return `${inject}${html}`;
}

/**
 * React hook: listen for postMessage link clicks from a sandboxed iframe
 * and open them in a new window. Validates event.source against the
 * provided iframe ref to prevent message spoofing.
 */
export function useIframeMessageHandler(
  iframeRef: React.RefObject<HTMLIFrameElement | null>,
  channelId?: string,
) {
  const handlerRef = useRef<((e: MessageEvent) => void) | null>(null);

  useEffect(() => {
    handlerRef.current = (event: MessageEvent) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      if (event.data?.type !== 'ksadk:linkClick') return;
      if (channelId && event.data?.channelId !== channelId) return;
      const url = event.data?.href;
      if (typeof url === 'string' && url && isSafeLinkHref(url)) {
        window.open(url, '_blank', 'noopener,noreferrer');
      }
    };

    window.addEventListener('message', handlerRef.current);
    return () => {
      if (handlerRef.current) {
        window.removeEventListener('message', handlerRef.current);
      }
    };
  }, [iframeRef, channelId]);
}
