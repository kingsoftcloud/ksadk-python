export class ApiError extends Error {
  code: number;
  detail?: unknown;

  constructor(code: number, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.detail = detail;
  }
}

export class StreamError extends Error {
  event: string;
  raw?: string;

  constructor(event: string, raw?: string, message?: string) {
    super(message || `SSE 流中断于 ${event}`);
    this.name = 'StreamError';
    this.event = event;
    this.raw = raw;
  }
}

export class CancelledError extends Error {
  constructor(message = '请求已取消') {
    super(message);
    this.name = 'CancelledError';
  }
}

const API_BASE = '/agentengine/api/v1';

async function parseActionResponse<T>(response: Response): Promise<T> {
  let data: Record<string, unknown>;
  try {
    data = await response.json();
  } catch {
    throw new ApiError(-2, '响应格式异常');
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      (data?.Message as string) || `HTTP ${response.status}`,
      data,
    );
  }

  if (data?.Code !== undefined && data.Code !== 0) {
    throw new ApiError(
      data.Code as number,
      (data.Message as string) || '请求失败',
      data,
    );
  }

  return data.Data as T;
}

function rethrowIfNotCancelled(error: unknown): never {
  if (error instanceof DOMException && error.name === 'AbortError') {
    throw new CancelledError();
  }
  throw error;
}

/** 1. JSON POST — 普通 agentengine action */
export async function postJsonAction<T>(
  action: string,
  body: Record<string, unknown>,
  options?: { signal?: AbortSignal },
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: options?.signal,
    });
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }
  return parseActionResponse<T>(response);
}

/** 2. FormData POST — UploadFile、AddWorkspaceFile */
export async function postFormAction<T>(
  action: string,
  formData: FormData,
  options?: { signal?: AbortSignal },
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/${action}`, {
      method: 'POST',
      body: formData,
      signal: options?.signal,
    });
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }
  return parseActionResponse<T>(response);
}

/** 3. GET blob/text — GetWorkspaceFileContent、AttachmentContent */
export async function getResource(
  action: string,
  params: Record<string, string>,
  options?: { signal?: AbortSignal; asText?: boolean },
): Promise<Blob | string> {
  const qs = new URLSearchParams(params).toString();
  const url = qs ? `${API_BASE}/${action}?${qs}` : `${API_BASE}/${action}`;
  let response: Response;
  try {
    response = await fetch(url, { signal: options?.signal });
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }

  if (!response.ok) {
    throw new ApiError(response.status, `资源获取失败: ${response.statusText}`);
  }

  try {
    return options?.asText ? await response.text() : await response.blob();
  } catch {
    throw new ApiError(-3, '文件读取失败');
  }
}

/** 4. SSE stream — RunAgent / SubscribeRunEvents */
export async function streamAction(
  action: string,
  body: Record<string, unknown>,
  options?: { signal?: AbortSignal },
): Promise<ReadableStream<Uint8Array>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/${action}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(body),
      signal: options?.signal,
    });
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }

  if (!response.ok) {
    throw new ApiError(response.status, `流式请求失败: ${response.statusText}`);
  }

  if (!response.body) {
    throw new ApiError(-1, '无法获取响应流');
  }

  return response.body;
}

/** GET SSE stream — SubscribeRunEvents 用 query params */
export async function streamGetAction(
  action: string,
  params: Record<string, string>,
  options?: { signal?: AbortSignal },
): Promise<ReadableStream<Uint8Array>> {
  const qs = new URLSearchParams(params).toString();
  const url = qs ? `${API_BASE}/${action}?${qs}` : `${API_BASE}/${action}`;
  let response: Response;
  try {
    response = await fetch(url, {
      headers: { Accept: 'text/event-stream' },
      signal: options?.signal,
    });
  } catch (error) {
    throw rethrowIfNotCancelled(error);
  }

  if (!response.ok) {
    throw new ApiError(response.status, `流式订阅失败: ${response.statusText}`);
  }

  if (!response.body) {
    throw new ApiError(-1, '无法获取响应流');
  }

  return response.body;
}
