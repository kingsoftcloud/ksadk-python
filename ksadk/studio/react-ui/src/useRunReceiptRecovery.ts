import { useCallback, useEffect, useRef } from "react";
import type { ConversationController, ConversationId, OutboxEntry, OutboxStore } from "@kingsoftcloud/ksadk-web/conversation";
import { reconcileOutboxEntry, type RunReceiptReader } from "./runReceiptRecovery";

/** Reconcile at restore/transport boundaries, not on each streamed delta. */
export function useRunReceiptRecovery(options: {
  api: RunReceiptReader; controller: ConversationController; outbox?: OutboxStore;
  agentId: string; conversationId: ConversationId; enabled: boolean; streaming: boolean;
  revision: number; isRequestActive: (requestId: string) => boolean;
}) {
  const { api, controller, outbox, agentId, conversationId, enabled, streaming } = options;
  const activeCheck = useRef(options.isRequestActive);
  activeCheck.current = options.isRequestActive;
  const scope = useRef<AbortController | null>(null);
  const pending = useRef(new Map<string, { entry: OutboxEntry; signal: AbortSignal; promise: Promise<void> }>());
  useEffect(() => {
    const owner = new AbortController();
    scope.current = owner;
    return () => { owner.abort(); if (scope.current === owner) scope.current = null; };
  }, [api, controller, outbox, agentId, conversationId, enabled]);

  const confirm = useCallback(async (requestId: string, signal = scope.current?.signal) => {
    const entry = outbox?.get(requestId);
    if (!enabled || !signal || signal.aborted || !entry || !outbox) return;
    const prior = pending.current.get(requestId);
    if (prior?.entry === entry && !prior.signal.aborted) return prior.promise;
    const promise = reconcileOutboxEntry({ api, outbox, entry, agentId, conversationId,
      binding: controller.binding(conversationId), signal,
      isRequestActive: id => activeCheck.current(id),
    });
    const request = { entry, signal, promise };
    pending.current.set(requestId, request);
    try { await promise; }
    finally { if (pending.current.get(requestId) === request) pending.current.delete(requestId); }
  }, [api, controller, outbox, agentId, conversationId, enabled]);
  // Status changes unknown -> accepted do not schedule a second query. A new
  // invocation or an actual transport transition does; tokens and our update
  // notification do not. The existing outbox remains the only local ledger.
  const requests = outbox?.list(conversationId).filter(entry => entry.agentId === agentId
    && ["unknown", "sending"].includes(entry.status) && !activeCheck.current(entry.requestId)) || [];
  const signature = JSON.stringify(requests.map(entry => [entry.requestId, entry.invocationId, entry.nativeSessionId]));
  useEffect(() => {
    if (!enabled) return;
    const owner = new AbortController();
    const identities: string[][] = JSON.parse(signature);
    void Promise.all(identities.map(([id]) => confirm(id, owner.signal)));
    return () => owner.abort();
  }, [enabled, signature, streaming, confirm]);
  return confirm;
}
