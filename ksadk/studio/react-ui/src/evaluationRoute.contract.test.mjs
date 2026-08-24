import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

test("evaluation page is registered as a conditional view", () => {
  assert.match(appSource, /const evaluationRunId = parts\[0\] === "evaluations" && parts\[1\]/);
  assert.match(appSource, /view === "evaluations" && !evaluationRunId[\s\S]*<EvaluationsPage/);
  assert.match(appSource, /<EvaluationDetailPage/);
  assert.match(appSource, /window\.history\.pushState\(null, "", `#\/evaluations\/\$\{encodeURIComponent\(runId\)\}`\)/);
});

test("App does not load evaluation data for existing views", () => {
  assert.doesNotMatch(appSource, /apiFetch\("\/api\/v1\/evaluations"/);
});
