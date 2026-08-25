import assert from "node:assert/strict";
import { test } from "node:test";
import { AUTHORING_STAGE_TEXT, authoringStageText } from "./authoringStages.ts";

test("every authoring stage maps to one of the two-phase waiting texts", () => {
  const texts = new Set(Object.values(AUTHORING_STAGE_TEXT));
  assert.ok(texts.has("正在理解你的需求…"));
  assert.ok(texts.has("正在生成 Agent 配置…"));
  for (const stage of ["resolving_model", "generating", "validating", "correcting", "done", "failed"]) {
    assert.ok(AUTHORING_STAGE_TEXT[stage], `missing text for stage ${stage}`);
  }
});

test("early stages resolve model while later stages generate the patch", () => {
  assert.equal(authoringStageText("resolving_model"), "正在理解你的需求…");
  assert.equal(authoringStageText("generating"), "正在理解你的需求…");
  assert.equal(authoringStageText("validating"), "正在生成 Agent 配置…");
  assert.equal(authoringStageText("correcting"), "正在生成 Agent 配置…");
});

test("unknown or missing stage falls back to the first waiting text", () => {
  assert.equal(authoringStageText(null), "正在理解你的需求…");
  assert.equal(authoringStageText(undefined), "正在理解你的需求…");
  assert.equal(authoringStageText("nonsense"), "正在理解你的需求…");
});
