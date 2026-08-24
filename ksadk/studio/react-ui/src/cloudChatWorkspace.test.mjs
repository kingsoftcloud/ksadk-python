import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(import.meta.dirname, "components/CloudChatWorkspace.tsx"), "utf8");
const composerSource = readFileSync(resolve(import.meta.dirname, "components/ChatComposer.tsx"), "utf8");
const settingsSource = readFileSync(resolve(import.meta.dirname, "components/SettingsOverlay.tsx"), "utf8");

test("cloud chat renders the foreground RunAgent stream and keeps SessionEvent as recovery", () => {
  assert.match(source, /const \[waitingForResponse, setWaitingForResponse\] = useState\(false\)/);
  assert.match(source, /if \(!active \|\| !currentSessionId\) return/);
  assert.match(source, /sending \|\| waitingForResponse \? 1200 : 4000/);
  assert.match(source, /awaitingAcceptedSeqRef\.current/);
  assert.match(source, /\[runId, invocationId\]\.filter\(Boolean\)\.includes\(eventRunId\)/);
  assert.match(source, /const matchesAcceptedWindow = afterSeq > 0 && eventSeq > afterSeq/);
  assert.match(source, /if \(!matchesRun && !matchesAcceptedWindow\) continue/);
  assert.match(source, /const sendInFlightRef = useRef\(false\)/);
  assert.match(source, /const currentSessionIdRef = useRef\(""\)/);
  assert.match(source, /const waitingForResponseRef = useRef\(false\)/);
  assert.match(source, /waitingForResponse \|\| sendInFlightRef\.current/);
  assert.match(source, /sendInFlightRef\.current = true/);
  assert.match(source, /sendInFlightRef\.current = false/);
  assert.match(source, /const awaitingInvocationIdRef = useRef\(""\)/);
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
  assert.match(source, /messages\/stream/);
  assert.match(source, /Accept: "text\/event-stream"/);
  assert.match(source, /directStreamItemPatches/);
  assert.match(source, /delta\.reasoning_content/);
  assert.match(source, /delta\.tool_calls/);
  assert.match(source, /response\.output_text\.delta/);
  assert.match(source, /directStreamActiveRef/);
  assert.match(source, /item\.kind === "message" \|\| directKindsSeenRef/);
});

test("cloud chat exposes a failed run without implementation or credential copy", () => {
  assert.match(source, /cloud-chat-run-warning/);
  assert.match(source, /这次云端运行未完成/);
  assert.doesNotMatch(source, /可查看运行详情/);
  assert.doesNotMatch(source, /AK\/SK/);
  assert.doesNotMatch(settingsSource, /AK\/SK/);
  assert.doesNotMatch(settingsSource, /不可变 Bundle|deployment receipt|伪造身份/);
});

test("cloud composer reuses the shared controls and sends turn policy explicitly", () => {
  assert.match(source, /<ChatComposer/);
  assert.match(composerSource, /ComposerActionMenu/);
  assert.match(composerSource, /ApprovalModeMenu/);
  assert.match(composerSource, /ModelReasoningMenu/);
  assert.match(source, /toolApprovalMode: approvalMode/);
  assert.match(source, /collaborationMode/);
  assert.match(source, /goalObjective/);
  assert.match(source, /modelOptions: effectiveReasoningEffort/);
  assert.match(source, /type: "input_image"/);
  assert.match(source, /type: "input_file"/);
  assert.doesNotMatch(source, /<select/);
});

test("cloud chat normalizes projected approval lifecycle events", () => {
  assert.match(source, /interruptInfo\.approval_request_id/);
  assert.match(source, /resumeInput\.approval_request_id/);
  assert.match(source, /\["interaction\.requested", "approval_request", "response\.approval_request"\]/);
  assert.match(source, /frame\.InvocationId/);
  assert.match(source, /"approval_response"/);
});
