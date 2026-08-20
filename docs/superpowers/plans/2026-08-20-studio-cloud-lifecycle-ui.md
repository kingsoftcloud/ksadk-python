# Studio Cloud Lifecycle UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a truthful Studio React workflow for deterministic Bundle build, preproduction admission/deployment, instance status and rollback.

**Architecture:** The Studio Python API remains the UI's only backend. It reads local deployment receipts, asks the existing `CloudDeploymentGateway` for Server-projected instance state, and dispatches existing deployment/rollback operations. React renders those receipts in the absorbed Soft Block system; it never constructs cloud Agent requests or reports a lifecycle state that the API did not return.

**Tech Stack:** FastAPI, Pydantic contracts, filesystem-backed Studio workspace, React, TypeScript, Vitest, existing `StudioDataTable` and `MoreActionsMenu`.

## Global Constraints

- The only cloud creation actions are `CreateAgentArtifact` and existing `CreateAgentProduct`.
- Target environment is exactly `preproduction`; all status labels come from API records.
- Use the Soft Block system from `origin/agentkit-studio-phase1` selectively, with only `document` and `workbench` layouts.
- Do not expose or persist control-plane tokens, credential material, private headers or artifact URLs.
- Final acceptance requires a real authenticated browser deployment to preproduction, then a cloud runtime call and lifecycle status refresh.

---

### Task 1: Add a truthful deployment collection read model

**Files:**
- Modify: `ksadk/studio/cloud.py`
- Modify: `ksadk/studio/api.py`
- Test: `tests/studio/test_api.py`

**Interfaces:**
- Produces `CloudDeploymentService.list() -> list[DeploymentRecord]`.
- Produces `GET /api/v1/deployments` returning `{ "items": DeploymentRecord[] }`.
- Keeps `GET /api/v1/deployments/{deployment_id}` as the only refresh endpoint.

- [ ] **Step 1: Write failing API tests**

```python
response = client.get("/api/v1/deployments")
assert response.status_code == 200
assert [item["id"] for item in response.json()["items"]] == ["dep-new", "dep-old"]
```

Also create one malformed `.agentkit/deployments/not-a-receipt.txt` and assert it does not appear.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `uv run pytest tests/studio/test_api.py -k deployments -q`

Expected: failure because collection route and service method do not exist.

- [ ] **Step 3: Implement strict receipt listing**

```python
def list(self) -> list[DeploymentRecord]:
    directory = self.workspace.resolve(".agentkit/deployments")
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("dep_*.json"), key=lambda item: item.name, reverse=True):
        payload = json.loads(path.read_text(encoding="utf-8"))
        records.append(DeploymentRecord.model_validate(payload["record"]))
    return records
```

Catch only malformed individual receipts, log them without their contents, and continue. Add the collection route without calling `refresh`, so a list view cannot trigger unbounded upstream requests.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/studio/test_api.py -k deployments -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/studio/cloud.py ksadk/studio/api.py tests/studio/test_api.py
git commit -m "feat(studio): list cloud deployment receipts"
```

### Task 2: Absorb the visual foundation without importing behavior changes

**Files:**
- Create: `ksadk/studio/react-ui/src/kingdesign.css`
- Create: `ksadk/studio/react-ui/src/soft-block.css`
- Modify: `ksadk/studio/react-ui/src/main.tsx`
- Modify: `ksadk/studio/react-ui/src/App.tsx`
- Modify: `ksadk/studio/react-ui/src/responsive.css`
- Test: `ksadk/studio/react-ui/src/App.routes.test.ts`

**Interfaces:**
- Produces `PageHeaderActions` portal target `#pageHeaderActions`.
- Preserves `#/resources/{kind}`, Agent detail and edit deep links.
- Uses `data-layout="document"` or `data-layout="workbench"` only.

- [ ] **Step 1: Add a failing route/portal test**

```tsx
expect(parseStudioLocationHash("#/agents/demo")).toMatchObject({
  view: "agent-detail", detailAgentId: "demo",
});
expect(screen.getByTestId("page-header-actions")).toBeInTheDocument();
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `npm --prefix ksadk/studio/react-ui run test:ui -- --run src/App.routes.test.tsx`

Expected: failure because the route parser/portal are absent.

- [ ] **Step 3: Port only visual primitives and routing shell**

Copy the CSS token files and small portal/navigation components from `origin/agentkit-studio-phase1`. Do not copy its `builder.py`, `framework_run.py`, `capabilities.py`, settings behavior, or other runtime code. Refactor `App.tsx` to own hash parsing and global header; retain present API calls and current Agent selection semantics.

- [ ] **Step 4: Verify browser-independent UI checks**

Run: `npm --prefix ksadk/studio/react-ui run test:ui -- --run src/App.routes.test.tsx && npm --prefix ksadk/studio/react-ui run build`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/studio/react-ui/src
git commit -m "feat(studio): adopt unified workbench visual shell"
```

### Task 3: Make Build show immutable artifact facts

**Files:**
- Modify: `ksadk/studio/react-ui/src/pages/BuildsPage.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/BuildsPage.test.tsx`

**Interfaces:**
- Consumes existing build `id`, `status`, `bundleDigest`, `resolvedDigest`, `runtimeName` and `runtimeVersion`.
- Produces a stat strip and a Bundle fact chain; a failed/missing API result maps to `data-state="failed"`/`idle`.

- [ ] **Step 1: Write a failing Build view test**

```tsx
expect(screen.getByText("Bundle Digest")).toBeInTheDocument();
expect(screen.getByText("构建阶段")).toBeInTheDocument();
expect(screen.getByText("FAILED")).toHaveAttribute("data-state", "failed");
```

- [ ] **Step 2: Run it to verify failure**

Run: `npm --prefix ksadk/studio/react-ui run test:ui -- --run src/pages/BuildsPage.test.tsx`

Expected: FAIL before the fact chain is rendered.

- [ ] **Step 3: Render the read model**

Move the build primary action into `PageHeaderActions`. Render Agent, revision, current status, runtime and abbreviated digest in `.stat-strip`; use `Bundle → admission pending → instance pending` only as explicit not-yet-deployed facts. Keep technical operation events in a collapsible details block.

- [ ] **Step 4: Verify the Build view**

Run: `npm --prefix ksadk/studio/react-ui run test:ui -- --run src/pages/BuildsPage.test.tsx && npm --prefix ksadk/studio/react-ui run build`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/studio/react-ui/src/pages/BuildsPage.tsx ksadk/studio/react-ui/src/pages/BuildsPage.test.tsx
git commit -m "feat(studio): show immutable bundle build facts"
```

### Task 4: Implement truthful preproduction deployment lifecycle view

**Files:**
- Modify: `ksadk/studio/react-ui/src/pages/DeploymentsPage.tsx`
- Create: `ksadk/studio/react-ui/src/pages/DeploymentsPage.test.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/AgentDetailPage.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/AgentDetailPage.test.tsx`

**Interfaces:**
- Consumes `GET /api/v1/deployments`, `GET /api/v1/deployments/{id}`, `POST /api/v1/builds/{id}/deployments` and `POST /api/v1/deployments/{id}:rollback`.
- Uses operation terminal states `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`.
- Produces a refreshable deployment table and a rollback action that creates a new receipt.

- [ ] **Step 1: Write failing lifecycle tests**

```tsx
expect(await screen.findByText("instance-1")).toBeInTheDocument();
await user.click(screen.getByRole("button", { name: "刷新部署状态" }));
expect(apiFetch).toHaveBeenCalledWith("/api/v1/deployments/dep-1");
await user.click(screen.getByRole("menuitem", { name: "回滚到 Build build-previous" }));
expect(apiFetch).toHaveBeenCalledWith("/api/v1/deployments/dep-1:rollback", expect.objectContaining({ method: "POST" }));
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `npm --prefix ksadk/studio/react-ui run test:ui -- --run src/pages/DeploymentsPage.test.tsx src/pages/AgentDetailPage.test.tsx`

Expected: FAIL because deployment page has only an empty state.

- [ ] **Step 3: Implement lifecycle read/command UI**

Build a document page with stat strip, Bundle/Admission/Instance fact chain and `StudioDataTable`. Query the new collection endpoint; an individual refresh replaces only that row. The details menu requests rollback with a selected successful Build from the matching Agent, waits for an operation terminal state, then reloads the collection. Agent detail retains one primary action (open conversation), exposes Build and Deploy through header actions/menu, and links to the resulting deployment row.

- [ ] **Step 4: Verify UI and API paths**

Run: `npm --prefix ksadk/studio/react-ui run test:ui -- --run src/pages/DeploymentsPage.test.tsx src/pages/AgentDetailPage.test.tsx && uv run pytest tests/studio/test_api.py -k deployments -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/studio/react-ui/src/pages/DeploymentsPage.tsx ksadk/studio/react-ui/src/pages/DeploymentsPage.test.tsx ksadk/studio/react-ui/src/pages/AgentDetailPage.tsx ksadk/studio/react-ui/src/pages/AgentDetailPage.test.tsx
git commit -m "feat(studio): manage preproduction deployment lifecycle"
```

### Task 5: Verify the visual contract and real preproduction flow

**Files:**
- Test: `tests/studio/test_style_system.py`
- Test: `tests/studio/e2e/studio_responsive_smoke.py`
- Evidence: `docs/superpowers/evidence/phase1/`

**Interfaces:**
- Requires the preproduction Server/Gateway/Runtime/Operator release candidate.
- Requires a real Studio browser login whose upstream ingress injects authenticated user identity.

- [ ] **Step 1: Add/adjust static style tests**

```python
assert 'data-layout="data"' not in source
assert 'box-shadow:' not in soft_block_css.replace("box-shadow: none !important", "")
assert "pageHeaderActions" in app_source
```

- [ ] **Step 2: Run all local gates**

Run: `npm --prefix ksadk/studio/react-ui run build && npm --prefix ksadk/studio/react-ui run test:ui && uv run pytest tests/studio/test_style_system.py tests/studio/test_webui_soft_block_contract.py tests/studio/test_api.py -q`

Expected: PASS.

- [ ] **Step 3: Run browser E2E on preproduction**

Use a real browser session to create an Agent, build it, deploy the successful immutable Bundle to preproduction, wait for `READY`, invoke the cloud Agent, refresh its lifecycle view, then initiate and observe rollback. Capture only non-secret IDs/digests/status screenshots as evidence.

- [ ] **Step 4: Commit evidence only after real result**

```bash
git add docs/superpowers/evidence/phase1
git commit -m "test(studio): record preproduction lifecycle evidence"
```
