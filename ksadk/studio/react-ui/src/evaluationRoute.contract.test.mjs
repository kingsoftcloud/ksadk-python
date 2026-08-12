import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

test("evaluation page is registered as a conditional view", () => {
  assert.match(appSource, /view === "evaluations"\s*&&\s*<EvaluationsPage/);
});

test("App does not load evaluation data for existing views", () => {
  assert.doesNotMatch(appSource, /apiFetch\("\/api\/v1\/evaluations"/);
});
