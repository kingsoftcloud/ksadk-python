/** Observe host control receipts while preserving the published SSE stream. */
export type ControlRun = { agentId: string; sessionId: string; invocationId: string; runId: string;
  childControls?: { input: boolean; promotion: boolean; cancellation?: boolean } };

export function observeControlAdmission(
  stream: ReadableStream<Uint8Array<ArrayBufferLike>>,
  scope: Omit<ControlRun, "runId">,
  onReady: (run: ControlRun) => void,
  onEnd: () => void,
): ReadableStream<Uint8Array<ArrayBufferLike>> {
  const decoder = new TextDecoder();
  let pending = "";
  let dropping = false;
  let ended = false;
  const finish = () => { if (!ended) { ended = true; onEnd(); } };
  return stream.pipeThrough(new TransformStream({
    transform(chunk, controller) {
      pending += decoder.decode(chunk, { stream: true });
      // Inspect only bounded receipt frames. Large content frames still pass
      // untouched to ksadk-web; they cannot enable controls.
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(pending))) {
        const frame = pending.slice(0, boundary.index);
        pending = pending.slice(boundary.index + boundary[0].length);
        if (dropping) { dropping = false; continue; }
        if (frame.length > 65536) continue;
        const event = /^event:\s*(.+)$/m.exec(frame)?.[1]?.trim();
        if (event === "done" || ["response.completed", "response.failed", "response.cancelled"].includes(event || "")) {
          finish(); continue;
        }
        if (event !== "studio.run.control_ready" && event !== "message") continue;
        try {
          const data = JSON.parse(frame.split(/\r?\n/).filter(line => line.startsWith("data:"))
            .map(line => line.slice(5).trimStart()).join("\n"));
          if (event === "message") {
            if (["run.completed", "run.failed", "run.cancelled", "run.interrupted"].includes(data.EventType)
              && data.InvocationId === scope.invocationId && data.SessionId === scope.sessionId) finish();
            continue;
          }
          if (ended) continue;
          if (data.AgentId === scope.agentId && data.SessionId === scope.sessionId
            && data.InvocationId === scope.invocationId && typeof data.RunId === "string" && data.RunId) {
            const controls = data.ChildControls;
            const childControls = controls && typeof controls === "object"
              && typeof controls.input === "boolean" && typeof controls.promotion === "boolean"
              ? { input: controls.input, promotion: controls.promotion,
                ...(typeof controls.cancellation === "boolean" ? { cancellation: controls.cancellation } : {}) } : undefined;
            onReady({ ...scope, runId: data.RunId, ...(childControls ? { childControls } : {}) });
          }
        } catch { /* A malformed receipt never enables controls. */ }
      }
      if (pending.length > 65536) { pending = pending.slice(-2); dropping = true; }
      controller.enqueue(chunk);
    },
    flush() { finish(); },
  }));
}
