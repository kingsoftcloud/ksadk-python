import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./pages/CreatePage.tsx", import.meta.url), "utf8");
const editorSource = readFileSync(new URL("./pages/AgentEditor.tsx", import.meta.url), "utf8");
const detailSource = readFileSync(new URL("./pages/AgentDetailPage.tsx", import.meta.url), "utf8");

test("create page uses shared React primitives for previews and summary overlay", () => {
  assert.match(source, /<CodeViewer/);
  assert.match(source, /<StudioDrawer[\s\S]*title="配置摘要"/);
  assert.doesNotMatch(source, /authoring-inspection-json/);
  assert.doesNotMatch(source, /manifest-preview-header/);
  assert.doesNotMatch(source, /<aside[^>]+role="dialog"/);
  assert.doesNotMatch(source, /wizard-summary-backdrop/);
});

test("agent source previews use the shared highlighted code viewer", () => {
  assert.match(editorSource, /<CodeViewer[\s\S]*language="yaml"/);
  assert.doesNotMatch(editorSource, /<pre>\{manifest\}<\/pre>/);
  assert.match(detailSource, /<CodeViewer[\s\S]*language=\{tab === "curl" \? "bash" : "javascript"\}/);
  assert.doesNotMatch(detailSource, /className="code-sample"/);
});
