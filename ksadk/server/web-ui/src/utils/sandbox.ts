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

function sourceList(...sources: Array<string | undefined>): string {
  return Array.from(new Set(sources.filter((source): source is string => Boolean(source)))).join(' ');
}

function buildPreviewCsp(basePath?: string): string {
  const assetSource = basePath ? cspSourceForBasePath(basePath) : undefined;
  const scriptSources = sourceList("'unsafe-inline'", "'unsafe-eval'", "'self'", 'https:', assetSource);
  const renderAssetSources = sourceList("'self'", 'https:', assetSource);
  if (basePath) {
    return [
      "default-src 'none'",
      `script-src ${scriptSources}`,
      `style-src 'unsafe-inline' data: ${renderAssetSources}`,
      `img-src data: blob: ${renderAssetSources}`,
      `font-src data: ${renderAssetSources}`,
      `media-src data: blob: ${renderAssetSources}`,
      "worker-src blob:",
      "connect-src 'none'",
      "form-action 'none'",
      "base-uri 'self'",
    ].join('; ');
  }

  return [
    "default-src 'none'",
    `script-src ${scriptSources}`,
    `style-src 'unsafe-inline' data: ${renderAssetSources}`,
    `img-src data: blob: ${renderAssetSources}`,
    `font-src data: ${renderAssetSources}`,
    `media-src data: blob: ${renderAssetSources}`,
    "worker-src blob:",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'self'",
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
function ksadkScrollToHash(rawHref) {
  if (!rawHref || rawHref === '#') return;
  var rawId = rawHref.slice(1);
  var id = rawId;
  try {
    id = decodeURIComponent(rawId);
  } catch (_) {}
  var target = document.getElementById(id) || document.getElementsByName(id)[0];
  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}
document.addEventListener('click', function(event) {
  var target = event.target && event.target.closest ? event.target.closest('a') : null;
  if (!target) return;
  var rawHref = target.getAttribute('href') || '';
  if (rawHref.charAt(0) === '#') {
    event.preventDefault();
    ksadkScrollToHash(rawHref);
    return;
  }
  if (target.href) {
    event.preventDefault();
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
