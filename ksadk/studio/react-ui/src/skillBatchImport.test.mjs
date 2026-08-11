import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transformWithOxc } from "vite";

async function loadBatchModule() {
  let source;
  try {
    source = await readFile(new URL("./skillBatchImport.ts", import.meta.url), "utf8");
  } catch (error) {
    assert.fail(`skillBatchImport.ts must implement batch import: ${error.message}`);
  }
  const transformed = await transformWithOxc(source, "skillBatchImport.ts", { lang: "ts" });
  const uniqueSource = `${transformed.code}\n// test-instance-${Math.random()}`;
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(uniqueSource).toString("base64")}`;
  return import(moduleUrl);
}

test("imports every selected candidate in scan order with only confirmed overwrites", async () => {
  const batch = await loadBatchModule();
  const calls = [];
  const candidates = [
    { candidateId: "ready-a", status: "ready" },
    { candidateId: "conflict-b", status: "conflict" },
    { candidateId: "invalid-c", status: "invalid" },
  ];

  const summary = await batch.runSkillImportBatch({
    candidates,
    selectedIds: new Set(["ready-a", "conflict-b", "invalid-c"]),
    overwriteIds: new Set(["conflict-b", "invalid-c"]),
    commit: async (candidate, overwrite) => {
      calls.push([candidate.candidateId, overwrite]);
      return { name: candidate.candidateId };
    },
  });

  assert.deepEqual(calls, [["ready-a", false], ["conflict-b", true]]);
  assert.deepEqual(summary.succeededIds, ["ready-a", "conflict-b"]);
  assert.deepEqual(summary.failedIds, []);
});

test("records a failed candidate and continues the remaining batch", async () => {
  const batch = await loadBatchModule();
  const visited = [];
  const progress = [];

  const summary = await batch.runSkillImportBatch({
    candidates: [
      { candidateId: "first", status: "ready" },
      { candidateId: "broken", status: "ready" },
      { candidateId: "last", status: "ready" },
    ],
    selectedIds: new Set(["first", "broken", "last"]),
    overwriteIds: new Set(),
    commit: async candidate => {
      visited.push(candidate.candidateId);
      if (candidate.candidateId === "broken") throw new Error("candidate changed");
      return { name: candidate.candidateId };
    },
    onResult: (result, completed, total) => {
      progress.push([result.candidateId, result.status, completed, total]);
    },
  });

  assert.deepEqual(visited, ["first", "broken", "last"]);
  assert.deepEqual(summary.succeededIds, ["first", "last"]);
  assert.deepEqual(summary.failedIds, ["broken"]);
  assert.equal(summary.results[1].error, "candidate changed");
  assert.deepEqual(progress, [
    ["first", "succeeded", 1, 3],
    ["broken", "failed", 2, 3],
    ["last", "succeeded", 3, 3],
  ]);
});

test("eligible selection includes ready and conflict candidates only", async () => {
  const batch = await loadBatchModule();
  const selected = batch.eligibleSkillIds([
    { candidateId: "ready", status: "ready" },
    { candidateId: "conflict", status: "conflict" },
    { candidateId: "invalid", status: "invalid" },
  ]);

  assert.deepEqual([...selected], ["ready", "conflict"]);
});
