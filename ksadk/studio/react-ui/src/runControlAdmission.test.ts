import { describe, expect, it } from "vitest";
import { observeControlAdmission, type ControlRun } from "./runControlAdmission";

const scope = { agentId: "agent", sessionId: "session", invocationId: "inv" };
const receipt = (changes = {}) => `event: studio.run.control_ready\ndata: ${JSON.stringify({
  AgentId: "agent", SessionId: "session", InvocationId: "inv", RunId: "host-run", ...changes,
})}\n\n`;

async function inspect(chunks: string[]) {
  const ready: ControlRun[] = [];
  let ended = false;
  const source = new ReadableStream<Uint8Array>({ start(controller) {
    chunks.forEach(chunk => controller.enqueue(new TextEncoder().encode(chunk)));
    controller.close();
  } });
  const reader = observeControlAdmission(source, scope, run => ready.push(run), () => { ended = true; }).getReader();
  let output = "";
  for (;;) { const row = await reader.read(); if (row.done) break; output += new TextDecoder().decode(row.value); }
  return { ready, ended, output };
}

describe("host control admission", () => {
  it("recognizes a fragmented host receipt and preserves every stream byte", async () => {
    const original = `event: response.created\ndata: {"response":{"id":"inv"}}\n\n${receipt()}`;
    const row = await inspect([original.slice(0, 82), original.slice(82, 121), original.slice(121)]);
    expect(row).toEqual({ ready: [{ ...scope, runId: "host-run" }], ended: true, output: original });
  });
  it("does not treat response creation or wrong-scope receipts as control admission", async () => {
    const row = await inspect([
      "event: response.created\ndata: {}\n\n",
      receipt({ AgentId: "other" }), receipt({ SessionId: "other" }), receipt({ InvocationId: "other" }),
      "event: studio.run.control_ready\ndata: bad-json\n\n",
    ]);
    expect(row.ready).toEqual([]);
  });
  it("projects child controls only from a matched host receipt with strict booleans", async () => {
    const row = await inspect([receipt({ ChildControls: { input: true, promotion: false } })]);
    expect(row.ready[0].childControls).toEqual({ input: true, promotion: false });
    const malformed = await inspect([receipt({ ChildControls: { input: "true", promotion: true } })]);
    expect(malformed.ready[0].childControls).toBeUndefined();
    const foreign = await inspect([receipt({ AgentId: "other", ChildControls: { input: true, promotion: true } })]);
    expect(foreign.ready).toEqual([]);
  });
  it("passes a large content frame without allowing it to swallow the following receipt", async () => {
    const content = `event: response.output_text.delta\ndata: ${"x".repeat(70000)}`;
    const row = await inspect([content, "\n\n", receipt()]);
    expect(row.ready).toHaveLength(1);
    expect(row.output).toBe(content + "\n\n" + receipt());
  });
  it("retires admission at a terminal frame even when the shared reader stops before EOF", async () => {
    const terminal = `event: message\ndata: ${JSON.stringify({ EventType: "run.completed", SessionId: "session", InvocationId: "inv" })}\n\n`;
    const row = await inspect([receipt(), terminal, receipt()]);
    expect(row.ready).toHaveLength(1);
    expect(row.ended).toBe(true);
  });
});
