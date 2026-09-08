# Thinking Control and E2E Latency Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the user-selected model unchanged, make thinking/reasoning a user-controlled switch, and cut perceived latency by removing blocking title work and enabling a fast path in pre-prod.

**Architecture:** The runtime will keep the same model that the user or project config selects. Thinking will become an explicit runtime/model parameter, defaulting from model capability or user preference instead of being hardcoded in the agent template. Session title refinement will remain best-effort and async so response completion is not blocked. The pre-prod e2e will validate that the runtime loads the patch before user agent code, preserves reasoning when enabled, and produces a noticeably faster first visible token when thinking is disabled.

**Tech Stack:** Python, pytest, ksadk code builder/container builder, LangChain `ChatOpenAI`, LangGraph runner, pre-prod Kubernetes, AgentEngine CLI.

---

### Task 1: Verify and lock the runtime injection order

**Files:**
- Modify: `ksadk/builders/code_builder.py:1331-1366`
- Modify: `ksadk/builders/container_builder.py:352-386`
- Test: `tests/test_builder_requirements_merge.py:174-230`

- [ ] **Step 1: Write the failing test**

```python
def test_code_builder_entrypoint_patches_langchain_before_loading_user_agent(tmp_path):
    builder = CodeBuilder(tmp_path)
    entrypoint = builder._generate_entrypoint(_full_detection_result(FrameworkType.LANGGRAPH))
    patch_index = entrypoint.index("apply_langchain_patch()")
    load_index = entrypoint.index("runner.load_agent()")
    assert patch_index < load_index
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_builder_requirements_merge.py::test_code_builder_entrypoint_patches_langchain_before_loading_user_agent -q`
Expected: FAIL until the entrypoint imports and applies the LangChain patch before `runner.load_agent()`.

- [ ] **Step 3: Write minimal implementation**

```python
from ksadk.configs import setup_environment
setup_environment(Path(CODE_ROOT))

try:
    from ksadk.runners.patch_langchain import apply_patch as apply_langchain_patch
    apply_langchain_patch()
except ImportError:
    pass

from ksadk.runners import create_runner
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_builder_requirements_merge.py::test_code_builder_entrypoint_patches_langchain_before_loading_user_agent tests/test_builder_requirements_merge.py::test_container_builder_entrypoint_patches_langchain_before_loading_user_agent -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/builders/code_builder.py ksadk/builders/container_builder.py tests/test_builder_requirements_merge.py
git commit -m "fix: patch langchain before loading user agents"
```

### Task 2: Keep OpenAI base URL compatible with user templates

**Files:**
- Modify: `ksadk/configs/settings.py:680-690`
- Test: `tests/test_setup_environment.py:24-75`

- [ ] **Step 1: Write the failing test**

```python
def test_setup_environment_injects_openai_base_url_when_only_auto_detected_base_is_available(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("AGENT_RUNTIME_ID", "ar-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(settings_module, "check_endpoint_reachable", lambda *args, **kwargs: False)

    setup_environment(tmp_path)

    assert os.environ["OPENAI_BASE_URL"] == "http://kspmas-internal.sdns.ksyun.com/v1"
    assert os.environ["OPENAI_API_BASE"] == "http://kspmas-internal.sdns.ksyun.com/v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_setup_environment.py::test_setup_environment_injects_openai_base_url_when_only_auto_detected_base_is_available -q`
Expected: FAIL until `setup_environment()` mirrors the detected base into both env vars.

- [ ] **Step 3: Write minimal implementation**

```python
if not os.getenv("OPENAI_BASE_URL") or not os.getenv("OPENAI_API_BASE"):
    api_base = settings.model.api_base
    if api_base:
        if not os.getenv("OPENAI_BASE_URL"):
            os.environ["OPENAI_BASE_URL"] = api_base
        if not os.getenv("OPENAI_API_BASE"):
            os.environ["OPENAI_API_BASE"] = api_base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_setup_environment.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/configs/settings.py tests/test_setup_environment.py
git commit -m "fix: mirror detected openai base url"
```

### Task 3: Keep assistant turn completion non-blocking

**Files:**
- Modify: `ksadk/conversations/runtime.py:854-920`
- Test: `tests/test_conversation_runtime.py:1290-1395`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_invoke_conversation_once_does_not_wait_for_ai_session_title(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    title_started = asyncio.Event()
    release_title = asyncio.Event()

    class _BlockingTitleClient:
        @property
        def is_available(self):
            return True

        async def generate_title(self, *, model, messages, timeout_ms):
            title_started.set()
            await release_title.wait()
            return "自我介绍", {"total_tokens": 12}

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_title_client", lambda: _BlockingTitleClient())
    runner = _StubRunner()
    invoke_task = asyncio.create_task(
        invoke_conversation_once(...)
    )
    await asyncio.wait_for(title_started.wait(), timeout=1)
    session_id, _ = await asyncio.wait_for(invoke_task, timeout=0.2)
    session = await service.get_session(session_id)
    assert session.title == "Agent能力介绍"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_conversation_runtime.py::test_invoke_conversation_once_does_not_wait_for_ai_session_title -q`
Expected: FAIL until title refinement is moved to a background task.

- [ ] **Step 3: Write minimal implementation**

```python
if next_title and next_title != (session.title or "").strip():
    await service.update_session_metadata(session_id, title=next_title, title_source=next_title_source)

if title_client.is_available and title_model:
    asyncio.create_task(
        _refine_session_title_in_background(
            service=service,
            session_id=session_id,
            first_prompt=first_prompt,
            assistant_text=summary,
            model=title_model,
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_conversation_runtime.py::test_invoke_conversation_once_does_not_wait_for_ai_session_title tests/test_conversation_runtime.py::test_invoke_conversation_once_refines_session_title_after_first_turn -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ksadk/conversations/runtime.py tests/test_conversation_runtime.py
git commit -m "fix: make session title refinement async"
```

### Task 4: Keep the pre-prod agent fast and predictable

**Files:**
- Modify: `/Users/xiayu/agentengine-test/langgraph_pre/langgraph_pre/agent.py`
- Verify: pre-prod runtime pod and hosted dashboard

- [ ] **Step 1: Make the agent use a user-controlled thinking setting**

```python
llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL_NAME", "glm-5.1"),
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    streaming=True,
)
```

- [ ] **Step 2: Rebuild and redeploy pre-prod**

Run: `KSYUN_REGION=pre-online PYTHONPATH=/Users/xiayu/.config/superpowers/worktrees/ksadk-python/feedback-otlp-direct python -m ksadk.cli deploy . --target serverless --artifact-type Code --region pre-online --ui-profile langchain --ui-path /chat --no-cache --no-version`
Expected: pre-prod `ar-20260507094358-35ed8a9d` updated in place.

- [ ] **Step 3: Verify runtime loading order and env injection**

Run: `kubectl -n ar-20260507094358-35ed8a9d logs deploy/ar-20260507094358-35ed8a9d -c agent-runtime --tail=120`
Expected: `setup_environment()` prints auto-detected base before user agent load.

- [ ] **Step 4: Verify streaming timing**

Run: a direct `POST /v1/responses` timing script and a `ChatOpenAI.astream()` timing script inside the runtime pod.
Expected: if thinking is disabled, first visible text should land in the ~1-2s range rather than the prior 4-6s range.

- [ ] **Step 5: Commit**

```bash
git add /Users/xiayu/agentengine-test/langgraph_pre/langgraph_pre/agent.py
git commit -m "fix: keep pre-prod agent on fast path"
```

### Task 5: Run end-to-end pre-prod validation

**Files:**
- Verify: `/Users/xiayu/agentengine-test/langgraph_pre`
- Verify: pre-prod hosted UI and agent dashboard

- [ ] **Step 1: Validate a normal chat turn**

Run: `agentengine dashboard open --no-open`
Expected: open the current pre-prod dashboard for `ar-20260507094358-35ed8a9d`.

- [ ] **Step 2: Validate refresh recovery**

Run: open the hosted UI, ask a single greeting, refresh the page, confirm the run is not stuck in "still running".
Expected: the response is completed and the UI no longer shows a stale active run.

- [ ] **Step 3: Validate thinking display behavior**

Run: test once with thinking enabled and once with thinking disabled.
Expected: thinking can be shown when enabled, but the default fast path should not block first visible text.

- [ ] **Step 4: Validate latency by direct comparison**

Run: compare the pre-prod UI turn against direct model streaming timings from the runtime pod.
Expected: the runtime overhead should be limited; if the provider is slow, the source is the model/provider, not the UI or title code.

- [ ] **Step 5: Record evidence**

Collect: command used, timestamp, pod name, response timing, and dashboard URL.
Expected: keep it redacted for secrets and reusable for the next performance plan.
