import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AUTHORING_STAGE_COPY,
  authoringStageText,
  authoringStageTip,
  formatElapsed,
} from "./authoringStages.ts";

test("every authoring stage has copy with a main text", () => {
  const stages = [
    "resolving_model",
    "generating",
    "codex_writing",
    "validating",
    "correcting",
    "done",
    "failed",
  ];
  for (const stage of stages) {
    assert.ok(AUTHORING_STAGE_COPY[stage]?.text, `missing text for stage ${stage}`);
  }
});

test("codex_writing has its own visible copy and a reassurance tip", () => {
  assert.equal(authoringStageText("codex_writing"), "Codex 正在编写 Agent 配置…");
  assert.ok(authoringStageTip("codex_writing").includes("分钟"));
});

test("early stages resolve model while later stages generate the patch", () => {
  assert.equal(authoringStageText("resolving_model"), "正在理解你的需求…");
  assert.equal(authoringStageText("generating"), "正在理解你的需求…");
  assert.ok(authoringStageText("validating").includes("校验"));
});

test("unknown or missing stage falls back to the first waiting text", () => {
  assert.equal(authoringStageText(null), "正在理解你的需求…");
  assert.equal(authoringStageText(undefined), "正在理解你的需求…");
  assert.equal(authoringStageText("nonsense"), "正在理解你的需求…");
});

test("elapsed formatting covers seconds and minutes", () => {
  assert.equal(formatElapsed(45), "45s");
  assert.equal(formatElapsed(90), "1m30s");
  assert.equal(formatElapsed(120), "2m");
});
