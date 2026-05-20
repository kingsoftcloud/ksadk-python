import { useEffect, useRef } from 'react';

type SandboxOptions = {
  channelId?: string;
  basePath?: string;
};

/**
 * Build a sandboxed HTML document for iframe srcdoc.
 * Injects CSP and link click interception.
 * When basePath is provided, also injects <base> tag and relaxes CSP
 * so sibling workspace files (CSS, JS, images) can be loaded via the API.
 */
export function buildSandboxedHtml(html: string, options: SandboxOptions | string = {}): string {
  const opts = typeof options === 'string' ? { channelId: options } : options;
  const { channelId, basePath } = opts;

  const csp = basePath
    ? [
        "default-src 'none'",
        "script-src 'unsafe-inline' 'unsafe-eval' 'self'",
        "style-src 'unsafe-inline' data: 'self'",
        "img-src data: blob: 'self'",
        "font-src data: 'self'",
        "media-src data: blob: 'self'",
        "connect-src 'self'",
        "form-action 'none'",
      ].join('; ')
    : [
        "default-src 'none'",
        "script-src 'unsafe-inline' 'unsafe-eval'",
        "style-src 'unsafe-inline' data:",
        "img-src data: blob:",
        "font-src data:",
        "media-src data: blob:",
        "connect-src 'none'",
        "form-action 'none'",
      ].join('; ');

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
<\/script>`;

  const cspMeta = `<meta http-equiv="Content-Security-Policy" content="${csp}">`;
  const baseTag = basePath ? `<base href="${basePath}">` : '';

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
export function useIframeMessageHandler(iframeRef: React.RefObject<HTMLIFrameElement | null>) {
  const handlerRef = useRef<((e: MessageEvent) => void) | null>(null);

  useEffect(() => {
    handlerRef.current = (event: MessageEvent) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      if (event.data?.type !== 'ksadk:linkClick') return;
      const url = event.data?.href;
      if (typeof url === 'string' && url) {
        window.open(url, '_blank', 'noopener,noreferrer');
      }
    };

    window.addEventListener('message', handlerRef.current);
    return () => {
      if (handlerRef.current) {
        window.removeEventListener('message', handlerRef.current);
      }
    };
  }, [iframeRef]);
}
