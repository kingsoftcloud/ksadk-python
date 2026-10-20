import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import type { ToolDisplay } from "./ToolExecutionCard";

/** Follow the owned process even after its launcher RPC or parent turn ends. */
export function useBackgroundCommandOutput(runId?: string, taskId?: string) {
  const key = runId && taskId ? `${runId}/${taskId}` : "";
  const [latest, setLatest] = useState<{ key: string; display: ToolDisplay }>();
  const [failure, setFailure] = useState("");
  useEffect(() => {
    if (!runId || !taskId || !/^[a-f0-9]{64}$/.test(taskId)) return;
    let disposed = false, retries = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const poll = async () => {
      let retryable = true;
      try {
        const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(runId)}/background-commands/${taskId}/output`,
          { signal: controller.signal });
        if (!response.ok) {
          retryable = response.status >= 500 || [408, 429].includes(response.status);
          throw new Error("query failed");
        }
        const result = await response.json();
        if (result.taskId !== taskId || result.display?.background_task_id !== taskId
          || result.display?.version !== "tool-display/v1"
          || !["starting", "running", "completed", "failed", "cancelled", "timed_out", "output_limit", "unknown"].includes(result.display.background_status)) {
          retryable = false;
          throw new Error("invalid process observation");
        }
        if (disposed) return;
        retries = 0;
        setFailure("");
        setLatest({ key, display: result.display });
        if (["starting", "running"].includes(result.display.background_status)) {
          timer = setTimeout(() => void poll(), 1000);
        }
      } catch {
        if (disposed) return;
        // A failed observation is not a process termination. Keep the last
        // confirmed facts and retry the same owned handle without restarting.
        setFailure(key);
        if (retryable) timer = setTimeout(() => void poll(), Math.min(1000 * 2 ** retries++, 10000));
      }
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); controller.abort(); };
  }, [runId, taskId, key]);
  return { display: latest?.key === key ? latest.display : undefined, queryFailed: Boolean(key && failure === key) };
}
