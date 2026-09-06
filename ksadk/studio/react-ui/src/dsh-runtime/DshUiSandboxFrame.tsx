import { useEffect, useRef, useState } from "react";
import type { DshUiSessionCreateResponse } from "./dshUiSandbox";
import { attachDshUiSandbox } from "./dshUiSandbox";

// Refresh the session a bit before the hard expiry so the UI never serves a
// stale token to the relay endpoint.
const SESSION_REFRESH_LEAD_SECONDS = 60;

/**
 * Renders one DSH plugin's sandboxed-iframe UI surface for a live UI session.
 * Owns the iframe element; the channel is established via attachDshUiSandbox
 * on mount and torn down on unmount. When the session nears expiry, calls
 * onSessionExpired so the host can create a fresh session.
 */
export function DshUiSandboxFrame({
  session,
  title,
  onChannelDisposed,
  onSessionExpired,
}: {
  session: DshUiSessionCreateResponse;
  title: string;
  onChannelDisposed?: () => void;
  onSessionExpired?: (sessionId: string) => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [disposed, setDisposed] = useState(false);

  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const cleanup = attachDshUiSandbox(iframe, session, () => {
      setDisposed(true);
      onChannelDisposed?.();
    });
    return cleanup;
    // The session payload is immutable for this component's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.uiSessionId]);

  // Schedule a refresh just before the session expires so the relay never
  // receives a stale token.
  useEffect(() => {
    if (!onSessionExpired) return;
    const delayMs = Math.max(
      (session.expiresInSeconds - SESSION_REFRESH_LEAD_SECONDS) * 1000,
      0,
    );
    const timer = window.setTimeout(() => {
      onSessionExpired(session.uiSessionId);
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [session.uiSessionId, session.expiresInSeconds, onSessionExpired]);

  return (
    <iframe
      ref={iframeRef}
      title={title}
      src={session.frame.url}
      sandbox={session.frame.sandbox}
      referrerPolicy="no-referrer"
      // credentialless is required by the backend contract; React types may lag.
      {...({ credentialless: true } as object)}
      style={{ border: "none", width: "100%", height: "100%" }}
      data-dsh-ui-session={session.uiSessionId}
      data-disposed={disposed ? "true" : "false"}
    />
  );
}
