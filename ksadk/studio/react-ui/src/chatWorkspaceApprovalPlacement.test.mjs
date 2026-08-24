import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(import.meta.dirname, "components/ChatWorkspace.tsx"), "utf8");
const composerSource = readFileSync(resolve(import.meta.dirname, "components/ChatComposer.tsx"), "utf8");

test("pending approval is rendered above the composer while history is read-only", () => {
  assert.match(source, /function ComposerInteractionTray/);
  assert.match(source, /surface\.interaction\?\.status === "pending"/);
  assert.match(source, /surface\.interaction\?\.status !== "pending"/);
  assert.ok(
    source.indexOf("<ComposerInteractionTray") < source.indexOf("<ChatComposer"),
    "pending interaction tray must precede the composer",
  );
});

test("the shared composer owns the approval control without native selects", () => {
  assert.match(composerSource, /ApprovalModeMenu/);
  assert.doesNotMatch(composerSource, /<select/);
});
