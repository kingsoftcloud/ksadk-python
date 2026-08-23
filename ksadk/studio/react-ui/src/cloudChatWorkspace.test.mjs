import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(import.meta.dirname, "components/CloudChatWorkspace.tsx"), "utf8");
const settingsSource = readFileSync(resolve(import.meta.dirname, "components/SettingsOverlay.tsx"), "utf8");

test("cloud chat keeps polling until an admitted run reaches a terminal response", () => {
  assert.match(source, /const \[waitingForResponse, setWaitingForResponse\] = useState\(false\)/);
  assert.match(source, /if \(!active \|\| !currentSessionId\) return/);
  assert.match(source, /sending \|\| waitingForResponse \? 1200 : 4000/);
  assert.match(source, /awaitingAcceptedSeqRef\.current/);
  assert.match(source, /const matchesRun = Boolean\(runId\) && eventRunId === runId/);
  assert.match(source, /const matchesAcceptedWindow = afterSeq > 0 && eventSeq > afterSeq/);
  assert.match(source, /if \(!matchesRun && !matchesAcceptedWindow\) continue/);
  assert.match(source, /receipt\.accepted_seq/);
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
  assert.match(source, /const assistantIdsBeforeSendRef = useRef<Set<string>>\(new Set\(\)\)/);
  assert.match(source, /!assistantIdsBeforeSendRef\.current\.has\(message\.id\)/);
  assert.match(source, /\.map\(message => message\.id\)/);
  assert.match(source, /refreshSessions\(\)\.catch\(\(\) => \{\}\)/);
  assert.match(source, /const sessionId = currentSessionIdRef\.current \|\| await createSession\(\)/);
  assert.match(source, /currentSessionIdRef\.current = session\.id/);
  assert.match(source, /waitingForResponseRef\.current = true/);
  assert.match(source, /waitingForResponseRef\.current = false/);
});

test("cloud chat exposes a failed run without implementation or credential copy", () => {
  assert.match(source, /cloud-chat-run-warning/);
  assert.match(source, /这次云端运行未完成/);
  assert.doesNotMatch(source, /可查看运行详情/);
  assert.doesNotMatch(source, /AK\/SK/);
  assert.doesNotMatch(settingsSource, /AK\/SK/);
  assert.doesNotMatch(settingsSource, /不可变 Bundle|deployment receipt|伪造身份/);
});

test("cloud composer stays limited to attachments, model and approval controls", () => {
  assert.match(source, /aria-label="上传附件"/);
  assert.match(source, /aria-label="选择模型"/);
  assert.match(source, /aria-label="审批级别"/);
  assert.match(source, /toolApprovalMode: approvalMode/);
  assert.match(source, /type: "input_image"/);
  assert.match(source, /type: "input_file"/);
  assert.doesNotMatch(source, /option value="full"/);
});

test("cloud chat normalizes projected approval lifecycle events", () => {
  assert.match(source, /interruptInfo\.approval_request_id/);
  assert.match(source, /resumeInput\.approval_request_id/);
  assert.match(source, /\["interaction\.requested", "approval_request"\]/);
  assert.match(source, /frame\.InvocationId/);
  assert.match(source, /"approval_response"/);
});
