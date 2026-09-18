import { ApiFacadeImpl } from "@kingsoftcloud/ksadk-web/runtime";

type StreamOptions = { signal?: AbortSignal };

function abortError(): DOMException {
  return new DOMException("Studio conversation stream detached", "AbortError");
}

/**
 * Detach UI subscription cancellation from the HTTP request that owns a Run.
 *
 * ksadk-web uses one AbortSignal both for leaving a conversation and for
 * cancelling a Run. Studio must keep those meanings separate: a refresh,
 * remount, or session-list reconciliation may stop the local reader, but it
 * must not turn a durable backend Run into a user cancellation.
 */
export function detachableRunStream(
  source: ReadableStream<Uint8Array<ArrayBufferLike>>,
  signal?: AbortSignal,
): ReadableStream<Uint8Array<ArrayBufferLike>> {
  if (!signal) return source;
  const reader = source.getReader();
  let settled = false;
  let controller: ReadableStreamDefaultController<Uint8Array<ArrayBufferLike>> | null = null;

  const cleanup = () => signal.removeEventListener("abort", onAbort);
  const onAbort = () => {
    if (settled) return;
    settled = true;
    cleanup();
    void reader.cancel("ui-detached").catch(() => undefined);
    controller?.error(abortError());
  };

  return new ReadableStream<Uint8Array<ArrayBufferLike>>({
    start(next) {
      controller = next;
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    },
    async pull(next) {
      if (settled) return;
      try {
        const chunk = await reader.read();
        if (settled) return;
        if (chunk.done) {
          settled = true;
          cleanup();
          next.close();
        } else {
          next.enqueue(chunk.value);
        }
      } catch (error) {
        if (settled) return;
        settled = true;
        cleanup();
        next.error(error);
      }
    },
    async cancel(reason) {
      if (settled) return;
      settled = true;
      cleanup();
      await reader.cancel(reason);
    },
  });
}

/** Studio-owned guard around the shared Web API facade. */
export class StudioChatApiFacade extends ApiFacadeImpl {
  private userCancellationAuthorized = false;

  /** Authorize exactly one CancelRun request from an explicit user action. */
  authorizeUserCancellation(): void {
    this.userCancellationAuthorized = true;
  }

  override async runAgent(body: Record<string, unknown>, opts?: StreamOptions) {
    // Do not pass the UI lifecycle signal into fetch. The wrapper below still
    // detaches the reader immediately, while the backend Run remains durable.
    const stream = await super.runAgent(body);
    return detachableRunStream(stream, opts?.signal);
  }

  override async resumeRun(
    params: Parameters<ApiFacadeImpl["resumeRun"]>[0],
    opts?: StreamOptions,
  ) {
    const stream = await super.resumeRun(params);
    return detachableRunStream(stream, opts?.signal);
  }

  override async subscribeRunEvents(
    params: Parameters<ApiFacadeImpl["subscribeRunEvents"]>[0],
    opts?: StreamOptions,
  ) {
    const stream = await super.subscribeRunEvents(params);
    return detachableRunStream(stream, opts?.signal);
  }

  override async cancelRun(
    agentId: string,
    sessionId: string,
    invocationId: string,
    opts?: StreamOptions,
  ): Promise<unknown> {
    if (!this.userCancellationAuthorized) {
      return { Cancelled: false, Ignored: true, Reason: "no-user-cancel-intent" };
    }
    this.userCancellationAuthorized = false;
    return super.cancelRun(agentId, sessionId, invocationId, opts);
  }
}
