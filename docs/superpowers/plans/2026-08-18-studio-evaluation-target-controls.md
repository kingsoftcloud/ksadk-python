# Studio Evaluation Target Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove manual Dataset version entry and expose business-facing controls for each Studio evaluation Target type.

**Architecture:** Keep the existing immutable cloud Dataset catalog and internal `TargetRef` contract. Only the React form labels and controls change; selected catalog entries continue to provide the exact version and hashes used by the backend.

**Tech Stack:** React, TypeScript, Vitest, Testing Library, Vite, pytest.

---

### Task 1: Lock the form contract with tests

**Files:**
- Modify: `ksadk/studio/react-ui/src/pages/EvaluationsPage.test.tsx`

- [ ] **Step 1: Add assertions for the new behavior**

Assert that the cloud form has no `#evaluation-dataset-version` input, and that the selected Target type renders `Agent 地址`, `Agent 源码目录`, or `Studio Build` labels as appropriate.

- [ ] **Step 2: Run the focused UI tests and confirm the expected failures**

Run: `npm run test:ui -- src/pages/EvaluationsPage.test.tsx`
Expected: failures for the removed version input and old `Target locator` labels.

### Task 2: Implement the business controls

**Files:**
- Modify: `ksadk/studio/react-ui/src/pages/EvaluationsPage.tsx`

- [ ] **Step 1: Remove the standalone Dataset version input**

Keep `cloudDatasetVersion` as state derived from the selected Catalog item and submit it internally.

- [ ] **Step 2: Render Target-specific labels and placeholders**

Use `Agent 地址` for A2A, `Agent 源码目录` for Local Source, and `Studio Build` for Build selection while keeping `evaluation-locator` as the stable form control id.

- [ ] **Step 3: Run the focused UI tests and build**

Run: `npm run test:ui -- src/pages/EvaluationsPage.test.tsx` and `npm run build`.
Expected: all focused tests pass and Vite exits 0.

### Task 3: Update evaluation documentation

**Files:**
- Modify: `docs/evaluation/端云评测一期开发记录.md`
- Modify: `docs/evaluation/端云评测方案.md`

- [ ] **Step 1: Document the simplified Studio interaction**

State that Dataset version is selected from the immutable Dataset catalog rather than typed, and Target locator is an internal field derived from Target-specific controls.

- [ ] **Step 2: Run KsADK Studio regression tests**

Run: `pytest -q tests/studio/test_evaluation_shell.py`.

### Task 4: Commit and push both repositories

**Files:**
- KsADK: only the related UI/tests/docs plus already-existing phase-one KsADK changes intended by the user.
- agent-eval: the three modified Dataset catalog contract files.

- [ ] **Step 1: Review diffs and run final verification**

Run focused UI tests, UI build, KsADK Studio tests, and agent-eval focused unit tests.

- [ ] **Step 2: Commit each repository with a single themed commit**

Use non-force commits on `agentkit-eval-phase1` and `feat/edge-cloud-evalset-phase1`.

- [ ] **Step 3: Push both branches and verify remote heads**

Run `git push origin <branch>` and compare local/remote commit IDs.
