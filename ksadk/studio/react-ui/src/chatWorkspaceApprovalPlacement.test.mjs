import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(import.meta.dirname, "components/ChatWorkspace.tsx"), "utf8");

test("the shared timeline receives approval and HITL handlers", () => {
  assert.match(source, /onRespondToApproval=\{chat\.respondToApproval\}/);
  assert.match(source, /onSubmitAguiAction=\{chat\.submitAguiAction\}/);
  assert.match(source, /interactionRecords=\{chat\.interactionRecords\}/);
});

test("the shared composer owns pending interactions and turn policy", () => {
  assert.match(source, /<AgentConversationComposer/);
  assert.match(source, /pendingInteractions=\{chat\.pendingInteractions\}/);
  assert.match(source, /onRespondInteraction=/);
  assert.match(source, /approvalPolicy=\{chat\.uiCapabilities\.ApprovalPolicy\}/);
  assert.match(source, /thinkingEnabled=\{Boolean\(chat\.uiCapabilities\.Thinking\)\}/);
});

test("the Studio shell retains responsive session navigation", () => {
  assert.match(source, /chat-session-mobile-trigger/);
  assert.match(source, /chat-session-mobile-close/);
  assert.match(source, /aria-expanded=\{sessionPanelOpen\}/);
  assert.match(source, /<h1>\{agentName\}<\/h1>/);
});
