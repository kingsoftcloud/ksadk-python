import { useEffect, useRef, useState } from "react";
import type { DshUiSessionCreateResponse } from "./dshUiSandbox";
import { attachDshUiSandbox } from "./dshUiSandbox";

/**
 * Renders one DSH plugin's sandboxed-iframe UI surface for a live UI session.
 * Owns the iframe element; the channel is established via attachDshUiSandbox
 * on mount and torn down on unmount.
 */
export function DshUiSandboxFrame({
  session,
  title,
  onChannelDisposed,
}: {
  session: DshUiSessionCreateResponse;
  title: string;
  onChannelDisposed?: () => void;
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
