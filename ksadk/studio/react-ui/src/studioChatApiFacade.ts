import { AgentEngineClient, ApiFacadeImpl, type AgentEngineClientOptions } from "@kingsoftcloud/ksadk-web/runtime";
import { observeControlAdmission, type ControlRun } from "./runControlAdmission";
import { ChildControlError, ChildRunClient, type ChildInput } from "./childRunControl";
import { parseRunReceipt } from "./runReceiptRecovery";

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
  private userCancellationAuthorized = "";
  private readonly commandClient: AgentEngineClient;
  private readonly scopeAgentId: string;
  private readonly childClient: ChildRunClient;
  private readonly controlRuns = new Map<string, ControlRun>();
  private readonly controlOwners = new Map<string, symbol>();
  private readonly controlListeners = new Set<() => void>();

  subscribeControls = (listener: () => void) => {
    this.controlListeners.add(listener);
    return () => { this.controlListeners.delete(listener); };
  };
  controlRun = (sessionId: string | null) => sessionId ? this.controlRuns.get(sessionId) : undefined;

  private observeRun(stream: ReadableStream<Uint8Array<ArrayBufferLike>>, scope: Omit<ControlRun, "runId">) {
    const owner = Symbol();
    this.controlOwners.set(scope.sessionId, owner);
    this.controlRuns.delete(scope.sessionId);
    const notify = () => { this.controlListeners.forEach(listener => listener()); };
    notify();
    return observeControlAdmission(stream, scope, run => {
      if (this.controlOwners.get(scope.sessionId) !== owner) return;
      this.controlRuns.set(scope.sessionId, run);
      notify();
    }, () => {
      if (this.controlOwners.get(scope.sessionId) !== owner) return;
      this.controlOwners.delete(scope.sessionId);
      this.controlRuns.delete(scope.sessionId);
      notify();
    });
  }

  constructor(options?: AgentEngineClientOptions) {
    super(options);
    this.commandClient = new AgentEngineClient(options);
    this.scopeAgentId = options?.agentId || "";
    this.childClient = new ChildRunClient(options?.fetch || globalThis.fetch.bind(globalThis));
  }

  private requireChildControl(run: ControlRun, capability?: "input" | "promotion" | "cancellation") {
    const current = this.controlRuns.get(run.sessionId);
    if (run.agentId !== this.scopeAgentId || !current || current.runId !== run.runId
      || current.invocationId !== run.invocationId || current.agentId !== run.agentId) {
      throw new ChildControlError("当前会话已切换或运行已结束，请重新查看子任务。");
    }
    if (capability && current.childControls?.[capability] !== true) {
      throw new ChildControlError("当前运行未声明这项子任务控制能力。");
    }
  }
  async childTasks(run: ControlRun, signal?: AbortSignal) {
    this.requireChildControl(run);
    return this.childClient.childTasks(run, signal);
  }
  async cancelChild(run: ControlRun, childRunId: string, commandId: string) {
    this.requireChildControl(run, "cancellation");
    return this.childClient.cancelChild(run, childRunId, commandId);
  }
  async promoteChild(run: ControlRun, childRunId: string, commandId: string) {
    this.requireChildControl(run, "promotion");
    return this.childClient.promoteChild(run, childRunId, commandId);
  }
  async sendChildInput(run: ControlRun, childRunId: string, input: ChildInput) {
    this.requireChildControl(run, "input");
    return this.childClient.sendChildInput(run, childRunId, input);
  }

  /** Query an original submission without starting or resuming a Run. */
  async getRunReceipt(agentId: string, sessionId: string, invocationId: string, signal?: AbortSignal) {
    if (agentId !== this.scopeAgentId || !sessionId || !invocationId) {
      throw new Error("无法确定原消息的运行身份，请查看会话记录。");
    }
    const value = await this.commandClient.postJsonAction<unknown>("GetRunReceipt", {
      AgentId: agentId, SessionId: sessionId, InvocationId: invocationId,
    }, { signal });
    return parseRunReceipt(value, { agentId, sessionId, invocationId });
  }

  /** Same scoped RPC contract as ksadk-web's upcoming steering facade. */
  async steerRun(agentId: string, invocationId: string, text: string) {
    const receipt = await this.commandClient.postJsonAction<{
      InvocationId: string; RunId: string; Status: "steering"; MessageId?: string;
    }>("SteerRun", { AgentId: agentId, InvocationId: invocationId, Text: text });
    if (receipt.InvocationId !== invocationId || !receipt.RunId || receipt.Status !== "steering") {
      throw new Error("运行时未确认本轮纠偏，请查看会话状态。");
    }
    return receipt;
  }

  /** Authorize exactly one CancelRun request from an explicit user action. */
  authorizeUserCancellation(agentId: string, sessionId: string | null, invocationId?: string): void {
    this.userCancellationAuthorized = sessionId && invocationId
      ? JSON.stringify([agentId, sessionId, invocationId]) : "";
  }

  override async runAgent(body: Record<string, unknown>, opts?: StreamOptions) {
    // Do not pass the UI lifecycle signal into fetch. The wrapper below still
    // detaches the reader immediately, while the backend Run remains durable.
    const stream = await super.runAgent(body);
    return detachableRunStream(this.observeRun(stream, {
      agentId: String(body.AgentId || ""), sessionId: String(body.SessionId || ""),
      invocationId: String(body.InvocationId || ""),
    }), opts?.signal);
  }

  override async resumeRun(
    params: Parameters<ApiFacadeImpl["resumeRun"]>[0],
    opts?: StreamOptions,
  ) {
    const stream = await super.resumeRun(params);
    return detachableRunStream(this.observeRun(stream, {
      agentId: params.agentId, sessionId: params.sessionId, invocationId: params.invocationId || params.runId,
    }), opts?.signal);
  }

  override async subscribeRunEvents(
    params: Parameters<ApiFacadeImpl["subscribeRunEvents"]>[0],
    opts?: StreamOptions,
  ) {
    const stream = await super.subscribeRunEvents(params);
    return detachableRunStream(this.observeRun(stream, {
      agentId: this.scopeAgentId, sessionId: params.sessionId, invocationId: params.invocationId,
    }), opts?.signal);
  }

  override async cancelRun(
    agentId: string,
    sessionId: string,
    invocationId: string,
    opts?: StreamOptions,
  ): Promise<unknown> {
    if (this.userCancellationAuthorized !== JSON.stringify([agentId, sessionId, invocationId])) {
      return { Cancelled: false, Ignored: true, Reason: "no-user-cancel-intent" };
    }
    this.userCancellationAuthorized = "";
    return super.cancelRun(agentId, sessionId, invocationId, opts);
  }
}
