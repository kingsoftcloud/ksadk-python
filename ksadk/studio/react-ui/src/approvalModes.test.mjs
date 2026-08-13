import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transformWithOxc } from "vite";

async function loadApprovalModes() {
  const source = await readFile(new URL("./approvalModes.ts", import.meta.url), "utf8");
  const transformed = await transformWithOxc(source, "approvalModes.ts", { lang: "ts" });
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString("base64")}`;
  return import(moduleUrl);
}

test("exposes exactly three approval levels with risk as the safe default", async () => {
  const approval = await loadApprovalModes();

  assert.deepEqual(approval.APPROVAL_MODES.map(item => item.value), ["ask", "risk", "full"]);
  assert.deepEqual(approval.APPROVAL_MODES.map(item => item.label), ["请求批准", "帮我批准", "完全访问权限"]);
  assert.equal(approval.normalizeApprovalMode("ask"), "ask");
  assert.equal(approval.normalizeApprovalMode("full"), "full");
  assert.equal(approval.normalizeApprovalMode("unknown"), "risk");
});

test("keeps approval preference scoped to one agent", async () => {
  const approval = await loadApprovalModes();

  assert.equal(approval.approvalModeStorageKey("research-agent"), "agentkit-studio:approval:research-agent");
});
