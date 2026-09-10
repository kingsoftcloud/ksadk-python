import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./pages/CreatePage.tsx", import.meta.url), "utf8");
const inspectionSource = readFileSync(new URL("./components/AuthoringInspectionSummary.tsx", import.meta.url), "utf8");
const editorSource = readFileSync(new URL("./pages/AgentEditor.tsx", import.meta.url), "utf8");
const detailSource = readFileSync(new URL("./pages/AgentDetailPage.tsx", import.meta.url), "utf8");

test("Harness uses one runtime list and no source-graph or ADK context fallback", () => {
  assert.match(source, /const BUILTIN_RUNTIME_OPTIONS = \[\s*\{ value: "harness", label: "KsADK Harness" \}/);
  assert.match(source, /const RUNTIME_OPTIONS = BUILTIN_RUNTIME_OPTIONS;/);
  assert.match(source, /if \(runtime === "harness"\) \{\s*return \[automatic, \{ value: "ksadk"/);
  assert.match(editorSource, /const contextOwnershipOptions = runtime === "harness"/);
  assert.match(editorSource, /\.\.\.\(runtime === "harness" \? \[\] : \[\s*`    projectPath:/);
  assert.doesNotMatch(editorSource, /HarnessRuntimeAdapter/);
});

test("create page uses shared React primitives for previews and summary overlay", () => {
  assert.match(source, /<AuthoringInspectionSummary/);
  assert.match(inspectionSource, /<CodeViewer/);
  assert.match(source, /<StudioDrawer[\s\S]*title="配置摘要"/);
  assert.doesNotMatch(source, /authoring-inspection-json/);
  assert.doesNotMatch(source, /manifest-preview-header/);
  assert.doesNotMatch(source, /<aside[^>]+role="dialog"/);
  assert.doesNotMatch(source, /wizard-summary-backdrop/);
});

test("conversation authoring uses the shared aligned fields and state-aware Draft rail", () => {
  assert.match(source, /<FormField\s+label="生成模型 Profile"/);
  assert.match(source, /<FormField\s+label="Agent 可用模型"/);
  assert.match(source, /data-draft-state=\{/);
  assert.doesNotMatch(source, /<div className="field authoring-model-field">/);
});

test("agent source previews use the shared highlighted code viewer", () => {
  assert.match(editorSource, /<CodeViewer[\s\S]*language="yaml"/);
  assert.doesNotMatch(editorSource, /<pre>\{manifest\}<\/pre>/);
  assert.match(detailSource, /<CodeViewer[\s\S]*language=\{tab === "curl" \? "bash" : "javascript"\}/);
  assert.doesNotMatch(detailSource, /className="code-sample"/);
});
