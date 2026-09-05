import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { DshUiSessionCreateResponse } from "./dshUiSandbox";
import { disposeDshUiSession, requestDshUiSession } from "./dshUiSandbox";

/**
 * Holds the live DSH UI sessions created from sandbox-compatible profile
 * bundles. Sessions are created lazily the first time a plugin's extension
 * point is materialized, then reused; they are disposed when the owning
 * profile projection drops the plugin (via prune) or on provider unmount.
 */
export interface DshUiSessionState {
  readonly payload: DshUiSessionCreateResponse;
  readonly dispose: () => Promise<void>;
}

interface DshUiSessionContextValue {
  /** Live sessions keyed by pluginId. */
  sessions: ReadonlyMap<string, DshUiSessionState>;
  /**
   * Create (or reuse) a session for one sandbox-compatible bundle. Returns
   * null when creation is not possible; the error is surfaced via onError.
   */
  ensureSession(input: {
    pluginId: string;
    clientDigest: string;
    toolIds?: readonly string[];
  }): Promise<DshUiSessionState | null>;
  /** Drop sessions for plugins no longer present in the projection. */
  prune(activePluginIds: ReadonlySet<string>): void;
  error: string | null;
}

const DshUiSessionContext = createContext<DshUiSessionContextValue | null>(null);

export function DshUiSessionProvider({
  agentId,
  onError,
  children,
}: {
  agentId?: string;
  onError?: (pluginId: string, message: string) => void;
  children: ReactNode;
}) {
  const [sessions, setSessions] = useState<Map<string, DshUiSessionState>>(
    () => new Map(),
  );
  const [error, setError] = useState<string | null>(null);
  // Guards against double-creating a session for the same plugin while one is
  // in flight (e.g. two surfaces mounting simultaneously).
  const inflight = useRef(new Map<string, Promise<DshUiSessionState | null>>());

  const ensureSession = useCallback(
    async (input: {
      pluginId: string;
      clientDigest: string;
      toolIds?: readonly string[];
    }): Promise<DshUiSessionState | null> => {
      const existing = inflight.current.get(input.pluginId);
      if (existing) return existing;
      const pending = (async () => {
        try {
          const payload = await requestDshUiSession({
            pluginId: input.pluginId,
            clientDigest: input.clientDigest,
            agentId,
            toolIds: input.toolIds,
          });
          const state: DshUiSessionState = {
            payload,
            dispose: () => disposeDshUiSession(payload.uiSessionId),
          };
          setSessions(previous => {
            const next = new Map(previous);
            next.set(input.pluginId, state);
            return next;
          });
          setError(null);
          return state;
        } catch (cause) {
          const message =
            cause instanceof Error ? cause.message : "DSH UI session 创建失败";
          setError(message);
          onError?.(input.pluginId, message);
          return null;
        } finally {
          inflight.current.delete(input.pluginId);
        }
      })();
      inflight.current.set(input.pluginId, pending);
      return pending;
    },
    [agentId, onError],
  );

  const prune = useCallback((activePluginIds: ReadonlySet<string>) => {
    setSessions(previous => {
      let changed = false;
      const next = new Map(previous);
      for (const [pluginId, state] of previous) {
        if (!activePluginIds.has(pluginId)) {
          void state.dispose();
          changed = true;
          next.delete(pluginId);
        }
      }
      return changed ? next : previous;
    });
  }, []);

  const value = useMemo<DshUiSessionContextValue>(
    () => ({ sessions, ensureSession, prune, error }),
    [sessions, ensureSession, prune, error],
  );

  return (
    <DshUiSessionContext.Provider value={value}>
      {children}
    </DshUiSessionContext.Provider>
  );
}

export function useDshUiSessions(): DshUiSessionContextValue | null {
  return useContext(DshUiSessionContext);
}
