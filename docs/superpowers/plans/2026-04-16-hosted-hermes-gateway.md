# Hosted Hermes Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AgentEngine-hosted Hermes runtimes supervise the gateway inside the container and stop exposing desktop `systemd` daemon flows in hosted connect/setup.

**Architecture:** The Hermes container entrypoint becomes the local supervisor for `hermes gateway run --replace`, with bounded restart and shutdown forwarding. Hosted runtime also exports explicit env markers so the PTY child process and docs can treat hosted Hermes differently from bare-metal installs.

**Tech Stack:** Bash entrypoint supervision, FastAPI runtime wrapper, ksadk Hermes terminal CLI, pytest.

---

### Task 1: Lock hosted runtime behavior in tests

**Files:**
- Modify: `tests/test_hermes_runtime_template.py`
- Modify: `tests/test_cmd_hermes.py`

- [ ] **Step 1: Add failing hosted-runtime assertions for entrypoint defaults**

Add assertions that the rendered Hermes entrypoint:

```python
assert 'export HERMES_HOSTED_RUNTIME="${HERMES_HOSTED_RUNTIME:-1}"' in entrypoint
assert 'export TERM="${TERM:-xterm-256color}"' in entrypoint
assert 'while true; do' in entrypoint
assert 'hermes gateway run --replace' in entrypoint
assert 'GATEWAY_LOCAL_RESTART_MAX' in entrypoint
```

- [ ] **Step 2: Run the runtime template test**

Run: `pytest tests/test_hermes_runtime_template.py -q`
Expected: FAIL on missing hosted runtime env / supervision-loop assertions.

- [ ] **Step 3: Add failing hosted connect behavior test**

Add a command test that hosted connect resolves a `connect` session normally but
documents/labels hosted runtime mode separately from daemon install language.

```python
def test_hermes_connect_enters_remote_gateway_setup(monkeypatch):
    captured = {}
    async def _fake_connect(**kwargs):
        captured.update(kwargs)
        return 0
    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_connect)
    ...
    assert captured["mode"] == "connect"
```

Extend the surrounding coverage so hosted-mode text and hints are asserted.

- [ ] **Step 4: Run the command test file**

Run: `pytest tests/test_cmd_hermes.py -q`
Expected: FAIL on new hosted-mode expectation.

### Task 2: Supervise Hermes gateway in the container entrypoint

**Files:**
- Modify: `deploy/hermes/entrypoint.sh`
- Test: `tests/test_hermes_runtime_template.py`

- [ ] **Step 1: Add hosted runtime env defaults and gateway supervision helpers**

Implement shell helpers similar to OpenClaw hosted bootstrap:

```bash
export HERMES_HOSTED_RUNTIME="${HERMES_HOSTED_RUNTIME:-1}"
export TERM="${TERM:-xterm-256color}"
GATEWAY_LOCAL_RESTART_MAX="${GATEWAY_LOCAL_RESTART_MAX:-5}"
GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS="${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS:-2}"

start_gateway_process() {
  hermes gateway run --replace &
  HERMES_GATEWAY_PID=$!
  set +e
  wait "${HERMES_GATEWAY_PID}"
  local exit_code=$?
  set -e
  HERMES_GATEWAY_PID=""
  return "${exit_code}"
}

forward_gateway_shutdown() {
  if [[ -n "${HERMES_GATEWAY_PID:-}" ]]; then
    kill -TERM "${HERMES_GATEWAY_PID}" 2>/dev/null || true
    wait "${HERMES_GATEWAY_PID}" 2>/dev/null || true
    HERMES_GATEWAY_PID=""
  fi
}
```

- [ ] **Step 2: Replace the unmanaged background launch with a bounded restart loop**

Use a `while true` loop with restart budget and pod-fail escalation:

```bash
HERMES_GATEWAY_FAILURE_COUNT=0
trap 'forward_gateway_shutdown; kill "${HERMES_DASHBOARD_PID}" 2>/dev/null || true; exit 0' TERM INT EXIT

while true; do
  GATEWAY_EXIT_CODE=0
  start_gateway_process || GATEWAY_EXIT_CODE=$?
  if [[ "${GATEWAY_EXIT_CODE}" -eq 0 ]]; then
    sleep "${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS}"
    continue
  fi
  HERMES_GATEWAY_FAILURE_COUNT=$((HERMES_GATEWAY_FAILURE_COUNT + 1))
  if [[ "${HERMES_GATEWAY_FAILURE_COUNT}" -ge "${GATEWAY_LOCAL_RESTART_MAX}" ]]; then
    exit "${GATEWAY_EXIT_CODE}"
  fi
  sleep "${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS}"
done &
HERMES_GATEWAY_SUPERVISOR_PID=$!
```

Keep `uvicorn` as PID 1 child exec target, but ensure cleanup kills supervisor
and dashboard on shutdown.

- [ ] **Step 3: Run the runtime template tests again**

Run: `pytest tests/test_hermes_runtime_template.py -q`
Expected: PASS.

### Task 3: Make hosted Hermes connect/setup container-aware

**Files:**
- Modify: `deploy/hermes/runtime/app.py`
- Modify: `ksadk/cli/cmd_hermes.py`
- Modify: `tests/test_cmd_hermes.py`

- [ ] **Step 1: Ensure PTY child inherits hosted env markers**

Before `os.execvp(...)` in `deploy/hermes/runtime/app.py`, populate env defaults:

```python
os.environ.setdefault("HERMES_HOSTED_RUNTIME", "1")
os.environ.setdefault("TERM", "xterm-256color")
```

Keep command resolution unchanged: `connect -> ["hermes", "gateway", "setup"]`.

- [ ] **Step 2: Add hosted connect messaging in CLI help and status hints**

Adjust `ksadk/cli/cmd_hermes.py` text so hosted users are pointed to
container-managed gateway behavior, not daemon install expectations.

```python
"agentengine hermes connect <agent>   # 远端配置 Feishu/Weixin，gateway 由容器托管"
```

- [ ] **Step 3: Run command tests**

Run: `pytest tests/test_cmd_hermes.py tests/test_hermes_terminal.py tests/test_hermes_terminal_e2e.py -q`
Expected: PASS.

### Task 4: Document hosted gateway behavior

**Files:**
- Modify: `deploy/hermes/README.md`
- Modify: `deploy/hermes/README.md.template`

- [ ] **Step 1: Update runtime docs**

Add explicit hosted notes:

```md
- Hermes gateway is started automatically inside the container.
- Hosted Hermes does not use `systemd`/`launchd`; gateway restarts are handled by the container entrypoint.
- `agentengine hermes connect` configures messaging platforms only.
```

- [ ] **Step 2: Re-run relevant doc/template tests**

Run: `pytest tests/test_hermes_dockerfile.py tests/test_help_snapshots.py -q`
Expected: PASS.

### Task 5: End-to-end verification

**Files:**
- Modify: `deploy/hermes/Dockerfile` (only if required by implementation fallout)

- [ ] **Step 1: Run the focused Hermes suite**

Run:

```bash
pytest \
  tests/test_cmd_hermes.py \
  tests/test_hermes_terminal.py \
  tests/test_hermes_terminal_e2e.py \
  tests/test_hermes_runtime_template.py \
  tests/test_hermes_dockerfile.py \
  tests/test_help_snapshots.py -q
```

Expected: PASS.

- [ ] **Step 2: Remote smoke-check the hosted runtime**

Run:

```bash
cd /Users/xiayu/agentengine-test/hermes-pre
uv run --project /Users/xiayu/kingsoft/code/agent-sdk/agentengine/ksadk-python agentengine hermes exec ar-20260414181827-c53a0231 -- status
uv run --project /Users/xiayu/kingsoft/code/agent-sdk/agentengine/ksadk-python agentengine hermes connect ar-20260414181827-c53a0231
```

Expected:
- `status` shows gateway running under hosted runtime
- `connect` enters platform setup without attempting `systemd` install

- [ ] **Step 3: Commit**

```bash
git add \
  docs/superpowers/specs/2026-04-16-hosted-hermes-gateway-design.md \
  docs/superpowers/plans/2026-04-16-hosted-hermes-gateway.md \
  deploy/hermes/entrypoint.sh \
  deploy/hermes/runtime/app.py \
  deploy/hermes/README.md \
  deploy/hermes/README.md.template \
  ksadk/cli/cmd_hermes.py \
  tests/test_cmd_hermes.py \
  tests/test_hermes_runtime_template.py
git commit -m "feat: supervise hosted hermes gateway"
```
