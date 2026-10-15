import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { createResponseSseParser, type ResponseStreamEvent } from "./chatProtocol";

/** Minimal shape of the run records returned by `GET /api/v1/runs?sessionId=`. */
export interface RunUsageRecord {
  id?: string;
  sessionId?: string;
  status?: string;
  usage?: {
    reported?: boolean;
    reasoningOutputTokens?: number;
  };
}

function numberOrNull(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

/** Run-record fallback: the run's own last reported value. */
export function recordReasoningTokens(run: RunUsageRecord | undefined): number | null {
  if (!run?.usage?.reported) return null;
  return numberOrNull(run.usage.reasoningOutputTokens);
}

/**
 * Sum this run's `UsageReported.reasoning_tokens`.
 *
 * A run makes several model calls and Studio only keeps the *last* call in the
 * run record's usage, so the run's thinking consumption has to be summed over
 * its `usage.reported` events (the same events the run inspector reads).
 */
export function reasoningTokensFromEvents(events: Array<ResponseStreamEvent | Record<string, unknown>>): number | null {
  let total = 0;
  let reported = false;
  for (const event of events) {
    const record = event as Record<string, unknown>;
    if (String(record.type || "") !== "usage.reported") continue;
    const tokens = numberOrNull(record.reasoningTokens ?? record.reasoning_tokens);
    if (tokens === null) continue;
    total += tokens;
    reported = true;
  }
  return reported ? total : null;
}

export async function fetchRunReasoningTokens(runId: string): Promise<number | null> {
  const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/events`);
  if (!response.ok) return null;
  const parsed: ResponseStreamEvent[] = [];
  const parser = createResponseSseParser(event => parsed.push(event));
  parser.push(await response.text());
  parser.finish();
  return reasoningTokensFromEvents(parsed);
}

function delay(milliseconds: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

/**
 * This turn's reasoning tokens for the conversation area.
 *
 * Usage is only durable once the run reports it, so the number is reconciled
 * when a live turn settles (streaming true -> false) rather than polled during
 * the run.  Nothing is fetched on ordinary session opens or history replay.
 */
export function useTurnReasoningTokens(options: {
  sessionId: string | null;
  streaming: boolean;
  enabled?: boolean;
}): number | null {
  const { sessionId, streaming, enabled = true } = options;
  const [state, setState] = useState<{ sessionId: string; tokens: number | null } | null>(null);
  const wasStreaming = useRef(false);

  useEffect(() => {
    if (!enabled || !sessionId) return undefined;
    const settled = wasStreaming.current && !streaming;
    wasStreaming.current = streaming;
    if (streaming || !settled) {
      // A new turn must not display the previous turn's number.
      if (streaming) setState({ sessionId, tokens: null });
      return undefined;
    }
    const abort = new AbortController();
    let active = true;
    const reconcile = async () => {
      try {
        const response = await apiFetch(
          `/api/v1/runs?sessionId=${encodeURIComponent(sessionId)}`,
          { signal: abort.signal },
        );
        if (!response.ok) throw new Error("run list unavailable");
        const payload = await response.json();
        const runs: RunUsageRecord[] = Array.isArray(payload?.items) ? payload.items : [];
        const latest = runs[runs.length - 1];
        let tokens = latest?.id ? await fetchRunReasoningTokens(latest.id) : null;
        if (tokens === null && latest?.id) {
          // The run record may still be settling; one cheap retry.
          await delay(800);
          tokens = await fetchRunReasoningTokens(latest.id);
        }
        if (active) setState({ sessionId, tokens: tokens ?? recordReasoningTokens(latest) });
      } catch {
        if (active) {
          setState(previous => previous?.sessionId === sessionId ? previous : { sessionId, tokens: null });
        }
      }
    };
    void reconcile();
    return () => {
      active = false;
      abort.abort();
    };
  }, [enabled, sessionId, streaming]);

  return state && state.sessionId === sessionId ? state.tokens : null;
}
