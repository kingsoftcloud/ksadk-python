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
