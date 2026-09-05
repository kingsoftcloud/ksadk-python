import { apiFetch } from "../api";

/**
 * Browser-side manager for the DSH UI sandbox channel.
 *
 * Mirrors the backend contract in ``ksadk/studio/dsh_ui_sandbox.py`` and
 * ``ksadk/studio/api_plugin_routes.py``: the trusted parent (this page) creates
 * a UI session over HTTP, mounts the token-free opaque-origin frame, then hands
 * the capability token to the frame over a dedicated MessagePort. The token
 * never appears in the DOM, a URL, or any document the sandbox can read.
 */

export const DSH_UI_PROTOCOL_VERSION = "agentkit.dsh-ui/v1";
const DSH_UI_SESSIONS_PATH = "/api/v1/plugin-ecosystems/dsh/ui-sessions";

export interface DshUiAllowedTool {
  name: string;
  [key: string]: unknown;
}

export interface DshUiHostHandshake {
  protocolVersion: string;
  kind: "init";
  sessionId: string;
  sourceId: string;
  handshakeNonce: string;
  capabilityToken: string;
}

export interface DshUiExtensionPoint {
  type: string;
  id: string;
  label?: string;
  path?: string;
  workspaceTabId?: string;
  renderer?: {
    type: "sandboxed-iframe";
    frameUrl: string;
  };
  [key: string]: unknown;
}

export interface DshUiSessionCreateResponse {
  uiSessionId: string;
  sourceId: string;
  expiresInSeconds: number;
  protocolVersion: string;
  descriptorDigest: string;
  inventoryDigest: string;
  allowedTools: DshUiAllowedTool[];
  handshake: DshUiHostHandshake;
  frame: {
    url: string;
    sandbox: string;
    referrerPolicy: string;
    credentialless: boolean;
  };
  extensionPoints: DshUiExtensionPoint[];
}

export interface DshUiSessionHandle {
  readonly sessionId: string;
  readonly sourceId: string;
  readonly frameUrl: string;
  readonly extensionPoints: readonly DshUiExtensionPoint[];
  readonly allowedTools: readonly DshUiAllowedTool[];
  dispose(): Promise<void>;
}

export class DshUiSessionError extends Error {
  constructor(
    readonly code: string,
    message: string,
    cause?: unknown,
  ) {
    super(message);
    this.name = "DshUiSessionError";
    if (cause !== undefined) Object.assign(this, { cause });
  }
}

interface RelayEnvelope {
  protocolVersion: string;
  kind: "request";
  sessionId: string;
  capabilityToken: string;
  sourceId: string;
  requestId: string;
  method: string;
  payload: Record<string, unknown>;
}

interface RelaySuccess {
  protocolVersion: string;
  kind: "response";
  sessionId: string;
  requestId: string;
  ok: true;
  result: unknown;
}

interface RelayFailure {
  protocolVersion: string;
  kind: "response";
  sessionId: string;
  requestId: string;
  ok: false;
  error: { code: string; message: string };
}

const READY_TIMEOUT_MS = 15_000;

async function parseError(response: Response): Promise<DshUiSessionError> {
  let message = `DSH UI session request failed (${response.status})`;
  let code = "DSH_UI_HTTP_ERROR";
  try {
    const body = await response.json();
    if (body && typeof body === "object") {
      const detail = (body as Record<string, unknown>).detail ?? body;
      if (typeof (detail as Record<string, unknown>).message === "string") {
        message = (detail as Record<string, string>).message;
      }
      if (typeof (detail as Record<string, unknown>).code === "string") {
        code = (detail as Record<string, string>).code;
      }
    }
  } catch {
    /* keep defaults */
  }
  return new DshUiSessionError(code, message);
}

/**
 * POST a session create and return the raw server payload. Shared by the
 * high-level handle API and the session provider (which keeps the payload for
 * the frame + attach path).
 */
export async function requestDshUiSession(input: {
  pluginId: string;
  clientDigest: string;
  agentId?: string;
  toolIds?: readonly string[];
}): Promise<DshUiSessionCreateResponse> {
  const response = await apiFetch(DSH_UI_SESSIONS_PATH, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pluginId: input.pluginId,
      clientDigest: input.clientDigest,
      agentId: input.agentId,
      toolIds: input.toolIds ?? [],
    }),
  });
  if (!response.ok) throw await parseError(response);
  const session = (await response.json()) as DshUiSessionCreateResponse;
  if (session.protocolVersion !== DSH_UI_PROTOCOL_VERSION) {
    throw new DshUiSessionError(
      "DSH_UI_PROTOCOL_MISMATCH",
      `Unsupported DSH UI protocol ${session.protocolVersion}`,
    );
  }
  return session;
}

/** Delete a session server-side (best-effort; it also expires on its own). */
export async function disposeDshUiSession(sessionId: string): Promise<void> {
  try {
    await apiFetch(
      `${DSH_UI_SESSIONS_PATH}/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  } catch {
    /* dispose is best-effort; the session expires server-side */
  }
}

/**
 * Create a DSH UI session for one enabled sandbox-compatible client bundle.
 * The returned handle owns the session lifecycle; callers must dispose it.
 */
export async function createDshUiSession(input: {
  pluginId: string;
  clientDigest: string;
  agentId?: string;
  toolIds?: readonly string[];
}): Promise<DshUiSessionHandle> {
  const session = await requestDshUiSession(input);

  return {
    sessionId: session.uiSessionId,
    sourceId: session.sourceId,
    frameUrl: session.frame.url,
    extensionPoints: Object.freeze(session.extensionPoints.slice()),
    allowedTools: Object.freeze(session.allowedTools.slice()),
    dispose: () => disposeDshUiSession(session.uiSessionId),
  };
}

/**
 * Attach a sandbox frame, transfer the capability token over a fresh
 * MessagePort, and bridge the frame's requests to the host's HTTP relay.
 *
 * Returns a cleanup function that tears the channel down. The iframe element
 * itself is owned by the caller (the component that rendered it).
 */
export function attachDshUiSandbox(
  iframe: HTMLIFrameElement,
  session: DshUiSessionCreateResponse,
  onDispose?: () => void,
): () => void {
  const { handshake, uiSessionId, sourceId } = session;
  const parentOrigin = window.location.origin;
  const messagePath = `${DSH_UI_SESSIONS_PATH}/${encodeURIComponent(uiSessionId)}/messages`;
  let cleaned = false;
  let ready = false;
  const channel = new MessageChannel();

  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    window.removeEventListener("message", onReady);
    channel.port1.onmessage = null;
    channel.port1.close();
    channel.port2.close();
    onDispose?.();
  };

  const onReady = (event: MessageEvent) => {
    // The frame is opaque-origin; its postMessage arrives with origin "null"
    // and source === contentWindow. Accept only our session's ready frame.
    if (event.source !== iframe.contentWindow) return;
    const message = event.data as Record<string, unknown> | null;
    if (
      !message ||
      typeof message !== "object" ||
      message.protocolVersion !== DSH_UI_PROTOCOL_VERSION ||
      message.kind !== "ready" ||
      message.sessionId !== uiSessionId ||
      message.sourceId !== sourceId
    ) {
      return;
    }
    ready = true;
    window.removeEventListener("message", onReady);
  };

  const relay = async (envelope: RelayEnvelope) => {
    const response = await apiFetch(messagePath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sourceId,
        frameOrigin: "null",
        message: envelope,
      }),
    });
    if (!response.ok) throw await parseError(response);
    return (await response.json()) as RelaySuccess | RelayFailure;
  };

  channel.port1.onmessage = (event: MessageEvent) => {
    const envelope = event.data as RelayEnvelope;
    if (!envelope || envelope.kind !== "request") return;
    void relay(envelope)
      .then((reply: RelaySuccess | RelayFailure) => {
        if (!cleaned) channel.port1.postMessage(reply);
      })
      .catch((error: unknown) => {
        if (cleaned) return;
        const failure: RelayFailure = {
          protocolVersion: DSH_UI_PROTOCOL_VERSION,
          kind: "response",
          sessionId: uiSessionId,
          requestId: envelope.requestId,
          ok: false,
          error: {
            code: error instanceof DshUiSessionError ? error.code : "DSH_UI_RELAY_ERROR",
            message: error instanceof Error ? error.message : "DSH UI relay failed",
          },
        };
        channel.port1.postMessage(failure);
      });
  };

  window.addEventListener("message", onReady);

  const handshakeTimer = window.setTimeout(() => {
    if (!ready) cleanup();
  }, READY_TIMEOUT_MS);
  window.addEventListener("message", function stopTimerOnce(event) {
    if (event.source === iframe.contentWindow && (event.data as { kind?: string })?.kind === "ready") {
      window.clearTimeout(handshakeTimer);
      window.removeEventListener("message", stopTimerOnce);
    }
  });

  // Transfer the token once the frame document has loaded its bootstrap. The
  // iframe is same-origin in URL terms but opaque-origin in security terms;
  // we verify contentWindow before handing over the port.
  iframe.addEventListener("load", () => {
    if (cleaned) return;
    if (iframe.contentWindow === null) {
      cleanup();
      return;
    }
    iframe.contentWindow.postMessage(handshake, parentOrigin, [channel.port2]);
  });

  return cleanup;
}
