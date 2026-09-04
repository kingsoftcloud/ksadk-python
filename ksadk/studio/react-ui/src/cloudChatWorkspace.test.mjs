import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const appSource = readFileSync(resolve(import.meta.dirname, "App.tsx"), "utf8");
const workspaceSource = readFileSync(
  resolve(import.meta.dirname, "components/ChatWorkspace.tsx"),
  "utf8",
);

test("local and cloud targets mount the same shared conversation workspace", () => {
  assert.doesNotMatch(appSource, /CloudChatWorkspace/);
  assert.equal((appSource.match(/<ChatWorkspace/g) || []).length, 2);
  assert.match(appSource, /isCloudChat && selectedCloudDeployment/);
  assert.match(appSource, /!isCloudChat && currentAgentId/);
});

test("Studio delegates conversation behavior to ksadk-web 0.3.5 entrypoints", () => {
  assert.match(workspaceSource, /AgentConversationTimeline/);
  assert.match(workspaceSource, /AgentConversationComposer/);
  assert.match(workspaceSource, /useAgentChat/);
  assert.match(workspaceSource, /new ApiFacadeImpl\(\{ fetch: apiFetch, agentId \}\)/);
  assert.match(workspaceSource, /conversationClient: null/);
});

test("shared capabilities gate thinking, approvals, attachments, and cancellation", () => {
  assert.match(workspaceSource, /uiCapabilities\.Thinking/);
  assert.match(workspaceSource, /uiCapabilities\.ApprovalPolicy/);
  assert.match(workspaceSource, /uiCapabilities\.Attachments/);
  assert.match(workspaceSource, /uiCapabilities\.StopRun/);
  assert.match(workspaceSource, /pendingInteractions=\{chat\.pendingInteractions\}/);
});
