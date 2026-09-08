let csrfToken = "";
const nativeFetch = window.fetch.bind(window);

function isProtectedStudioPath(pathname: string): boolean {
  return pathname.startsWith("/api/v1/")
    || pathname === "/v1/responses"
    || pathname.startsWith("/v1/responses/");
}

export async function apiFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const method = String(
    init.method || (input instanceof Request ? input.method : "GET"),
  ).toUpperCase();
  const url = new URL(
    input instanceof Request ? input.url : String(input),
    window.location.href,
  );
  const headers = new Headers(
    init.headers || (input instanceof Request ? input.headers : undefined),
  );

  if (
    csrfToken
    && url.origin === window.location.origin
    && isProtectedStudioPath(url.pathname)
    && !["GET", "HEAD", "OPTIONS"].includes(method)
  ) {
    headers.set("X-CSRF-Token", csrfToken);
  }

  const retryInput = input instanceof Request ? input.clone() : input;
  const requestInit = {
    ...init,
    headers,
    credentials: init.credentials || "same-origin",
  };
  const response = await nativeFetch(input, requestInit);
  if (
    response.status !== 403
    || ["GET", "HEAD", "OPTIONS"].includes(method)
    || url.origin !== window.location.origin
    || !isProtectedStudioPath(url.pathname)
  ) {
    return response;
  }

  let errorCode = "";
  try {
    errorCode = (await response.clone().json())?.error?.code || "";
  } catch {
    return response;
  }
  if (errorCode !== "CSRF_TOKEN_INVALID") return response;

  const bootstrap = await nativeFetch("/api/v1/system/bootstrap", {
    credentials: "same-origin",
  });
  if (!bootstrap.ok) return response;
  const payload = await bootstrap.json();
  csrfToken = payload.csrfToken || "";
  if (!csrfToken) return response;

  headers.set("X-CSRF-Token", csrfToken);
  return nativeFetch(retryInput, { ...requestInit, headers });
}

export async function initializeStudioSession(): Promise<void> {
  const match = window.location.hash.match(/(?:^#|&)session=([^&]+)/);
  if (match) {
    const response = await nativeFetch("/api/v1/system/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: decodeURIComponent(match[1]) }),
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error("本地 Studio 会话已失效，请重新启动服务。");
    }

    const payload = await response.json();
    csrfToken = payload.csrfToken || "";
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}`,
    );
    return;
  }

  const bootstrap = await nativeFetch("/api/v1/system/bootstrap", {
    credentials: "same-origin",
  });
  if (!bootstrap.ok) return;
  const payload = await bootstrap.json();
  csrfToken = payload.csrfToken || "";
}

export function currentCsrfToken(): string {
  return csrfToken;
}

// ---------------------------------------------------------------------------
// agent-kernel/v1 control surface. Contract decoders live in chatProtocol.ts
// and mirror @kingsoftcloud/ksadk-web's runtime bundle; the stream reducer is
// never duplicated.
// ---------------------------------------------------------------------------

import type { AgentControlReceipt } from "./chatProtocol.ts";

export interface SubmitAgentControlParams {
  commandType:
    | "enqueue" | "steer" | "inject" | "interrupt"
    | "pause" | "resume" | "submit_interaction";
  idempotencyKey: string;
  payload: Record<string, unknown>;
}

/** Submit an AgentControlCommand/v1 and strictly decode the receipt. */
export async function submitAgentControl(
  params: SubmitAgentControlParams,
  options?: { signal?: AbortSignal },
): Promise<AgentControlReceipt> {
  const response = await apiFetch("/api/v1/agent-control/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      schema_version: 1,
      command_type: params.commandType,
      idempotency_key: params.idempotencyKey,
      payload: params.payload,
      source: { kind: "studio", ref: "studio-react-ui" },
    }),
    credentials: "same-origin",
    signal: options?.signal,
  });
  if (!response.ok) {
    throw new Error(`SubmitAgentControl failed: HTTP ${response.status}`);
  }
  const payload = await response.json();
  // Deferred import keeps api.ts loadable in the node --test harness, which
  // evaluates this module from a data: URL where relative specifiers cannot
  // resolve. Vite bundles it as a normal chunk split.
  const { decodeReceipt } = await import("./chatProtocol.ts");
  return decodeReceipt(payload.Receipt ?? payload);
}

/**
 * Subscribe to session events from the last unified Session seq.
 * `afterSeq` comes from SessionEventCursor.reconnectAfterSeq().
 */
export async function subscribeSessionEvents(
  sessionId: string,
  afterSeq: number,
  options?: { signal?: AbortSignal },
): Promise<ReadableStream<Uint8Array>> {
  const params = new URLSearchParams({ SessionId: sessionId, AfterSeq: String(afterSeq) });
  const response = await apiFetch(`/api/v1/agent-control/events?${params}`, {
    headers: { Accept: "text/event-stream" },
    credentials: "same-origin",
    signal: options?.signal,
  });
  if (!response.ok) {
    throw new Error(`SubscribeSessionEvents failed: HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error("SubscribeSessionEvents 返回了空响应流");
  }
  return response.body;
}
