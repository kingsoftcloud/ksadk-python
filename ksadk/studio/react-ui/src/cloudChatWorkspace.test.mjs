import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(import.meta.dirname, "components/CloudChatWorkspace.tsx"), "utf8");

test("cloud chat keeps polling until an admitted run reaches a terminal response", () => {
  assert.match(source, /const \[waitingForResponse, setWaitingForResponse\] = useState\(false\)/);
  assert.match(source, /if \(!active \|\| !currentSessionId\) return/);
  assert.match(source, /sending \|\| waitingForResponse \? 1200 : 4000/);
  assert.match(source, /terminalRunEvent\(events, awaitingRunIdRef\.current\)/);
  assert.match(source, /const sendInFlightRef = useRef\(false\)/);
  assert.match(source, /const currentSessionIdRef = useRef\(""\)/);
  assert.match(source, /const waitingForResponseRef = useRef\(false\)/);
  assert.match(source, /waitingForResponse \|\| sendInFlightRef\.current/);
  assert.match(source, /sendInFlightRef\.current = true/);
  assert.match(source, /sendInFlightRef\.current = false/);
  assert.match(source, /payload\.invocation_id/);
  assert.match(source, /frame\.invocation_id/);
  assert.match(source, /\["run_status", "run\.status"\]/);
  assert.match(source, /content\.status/);
  assert.match(source, /rows\.filter\(message => message\.role === "assistant"\)\.length > assistantCountBeforeSendRef\.current/);
  assert.match(source, /message => !message\.pending && message\.role === "assistant"/);
  assert.match(source, /const sessionId = currentSessionIdRef\.current \|\| await createSession\(\)/);
  assert.match(source, /currentSessionIdRef\.current = session\.id/);
  assert.match(source, /waitingForResponseRef\.current = true/);
  assert.match(source, /waitingForResponseRef\.current = false/);
});

test("cloud chat exposes a failed run and keeps its credentials in the Studio backend", () => {
  assert.match(source, /cloud-chat-run-warning/);
  assert.match(source, /这次云端运行未完成/);
  assert.doesNotMatch(source, /可查看运行详情/);
  assert.match(source, /AK\/SK 仅保留在本地 Studio 进程/);
});
