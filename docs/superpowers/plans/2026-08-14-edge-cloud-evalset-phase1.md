# Edge-Cloud EvalSet Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a validated local EvalSet as an account-scoped, versioned cloud Dataset, then inspect and export a fixed Dataset version without changing local evaluation execution.

**Architecture:** Keep `EvalSetVersion` as the local source contract. A new evaluation cloud layer performs conversion, data-policy preview, binding persistence, and HTTP orchestration; CLI and Studio only call that layer. The agent-eval API must expose an atomic snapshot publication and fixed-version reads before UI publication is enabled.

**Tech Stack:** Python 3.10, Pydantic v2, Click, FastAPI, httpx, React/Vite, pytest.

---

### Task 1: Pure EvalSet cloud conversion and binding

**Files:**
- Create: `ksadk/evaluation/cloud_converter.py`
- Create: `ksadk/evaluation/cloud_binding.py`
- Create: `tests/test_evaluation_cloud_converter.py`
- Create: `tests/test_evaluation_cloud_binding.py`

- [ ] Add failing tests for a multi-turn native EvalSet converting to fixed columns and rows, then converting back with an equal content digest.
- [ ] Add failing tests that reject malformed remote rows and verify a binding never contains credential material.
- [ ] Implement only pure conversion and atomic YAML binding persistence.
- [ ] Run `pytest -q tests/test_evaluation_cloud_converter.py tests/test_evaluation_cloud_binding.py`.
- [ ] Commit: `feat(eval): add cloud evalset converter and binding`.

### Task 2: Cloud preview and HTTP client

**Files:**
- Create: `ksadk/evaluation/cloud_client.py`
- Create: `ksadk/evaluation/cloud_service.py`
- Create: `tests/test_evaluation_cloud_service.py`

- [ ] Add tests for preview, local-only network denial, idempotency propagation, and no binding update after a failed publish.
- [ ] Implement a typed client for the frozen agent-eval snapshot API; do not call create-plus-import endpoints.
- [ ] Implement publish/list/describe/export service methods around the client and converter.
- [ ] Run the cloud unit tests.
- [ ] Commit: `feat(eval): add cloud evalset publish service`.

### Task 3: agent-eval snapshot contract

**Files:**
- Modify: `services/agent-eval/src/agent_eval/schemas/eval.py`
- Modify: `services/agent-eval/src/agent_eval/api/v1/endpoints/datasets.py`
- Modify: `services/agent-eval/src/agent_eval/services/eval_dataset_service.py`
- Test: `services/agent-eval/tests/...dataset snapshot contract...`

- [ ] Add a single atomic snapshot publish request with `IdempotencyKey`, `ContentDigest`, optional `BaseVersion`, columns, and rows.
- [ ] Reject base-version conflicts without replacing current rows; repeated key/digest returns the previous result.
- [ ] Add `DatasetVersion` to dataset detail reads and guarantee the selected version is returned.
- [ ] Add account visibility/isolation and no-partial-snapshot tests.
- [ ] Commit independently in `agent-eval` after its own test suite passes.

### Task 4: CLI

**Files:**
- Create: `ksadk/cli/cmd_evalset.py`
- Modify: `ksadk/cli/__init__.py`
- Create: `tests/cli/test_cmd_evalset.py`

- [ ] Add failing Click tests for `preview`, `push`, `list`, `describe`, and `export`.
- [ ] Implement a standalone `agentengine evalset` group backed only by `cloud_service`.
- [ ] Run command help and CLI regression tests.
- [ ] Commit: `feat(cli): add evalset cloud commands`.

### Task 5: Studio

**Files:**
- Create: `ksadk/studio/evaluation_cloud_service.py`
- Modify: `ksadk/studio/api.py`
- Modify: `ksadk/studio/react-ui/src/api.ts`
- Modify: `ksadk/studio/react-ui/src/pages/EvaluationsPage.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/evaluations.css`
- Test: `tests/studio/test_evaluation_cloud_api.py`
- Test: `ksadk/studio/react-ui/src/pages/EvaluationsPage.test.tsx`

- [ ] Add tests proving API routes stay scoped to evaluation-cloud endpoints and existing evaluation submission behavior is unchanged.
- [ ] Add preview, publish, list, detail, and export actions to the evaluation page only.
- [ ] Verify the React route split and non-evaluation Studio pages remain unchanged.
- [ ] Commit: `feat(studio): manage cloud evalsets`.

### Task 6: End-to-end verification and documentation

**Files:**
- Modify: `docs/evaluation/端云评测方案.md`
- Create: `docs/evaluation/端云评测验证记录.md`

- [ ] Run unit, CLI, Studio API, React, and real agent-eval environment tests.
- [ ] Record exact commands, account-isolation evidence, and unavailable production prerequisites.
- [ ] Re-fetch both repositories and review diffs before integration commits.

