# RuntimeEvent Schema v2 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the identity-aware `RuntimeEvent(schema_version=2)` pipeline across KsADK, its protocol adapters, Studio, ksadk-web, and Hosted UI, then pass the 0.8.1 release gates without requiring an AgentEngine server upgrade.

**Architecture:** Source-specific adapters preserve native scope/item/occurrence identities and emit one canonical event union. One durable store and one `StreamReducer` drive live delivery, replay, final-output selection, Responses, AG-UI/A2UI, A2A, Studio, session projections, and the short-lived v1 projector. Legacy v1 output is read-only: undeclared consumers receive terminal snapshots, while upgraded consumers explicitly opt into identity-aware replace semantics.

**Tech Stack:** Python 3.10-3.14, Pydantic 2, pytest/pytest-asyncio, Google ADK 2.6.3+, LangGraph 1.2.x event streaming v3, A2A SDK 1.1.0, Codex app-server/openai-codex 0.144.4, React 19, TypeScript 5.9, Zustand 5, Vitest 4, Vite 8.

## Global Constraints

- The canonical public Python type is `RuntimeEvent` with `schema_version=2`; do not introduce `RuntimeEventV2` or retain a top-level v1 alias.
- Legacy names exist only in `ksadk/events/v1_compat.py`: `RuntimeEventV1`, `RuntimeEventV1Parser`, and `project_to_v1`.
- Canonical storage accepts schema v2 only. Do not dual-write v1 and v2 and do not migrate historical v1 events into canonical v2 events.
- `scope_id`, `item_id`, and `event_id` are deterministic source-derived identities. Never derive them from author, text, hashes of text, timestamps, UUIDs, or process-local counters.
- One item is one message, reasoning segment, tool call, tool result, artifact, or data surface; blocks within that item use `part_id`.
- `event_id` idempotency is scoped by `(session_id, event_id)` and is enforced before sequence allocation and publication. Session `seq` remains the delivery/replay cursor.
- The reducer never uses `startswith`, common-prefix/suffix overlap, text hash, adjacent-message guesses, per-agent snapshots, or per-author snapshots.
- `item.completed.snapshot` is authoritative. Different items with identical text must both remain visible.
- Only LangGraph `graph_checkpoint` projects to legacy `run_checkpoint`; ADK invocation resume, Codex thread resume, and task resume remain distinct continuation kinds.
- A2A peers without occurrence identity are provisional during live push and must reconcile from terminal `GetTask` snapshots.
- `RunAgent` remains Responses/Chat Completions wire compatible. AgentEngine server must not parse raw RuntimeEvent v1/v2 and is not modified in this plan.
- Existing local uncommitted work in `ksadk-web` (`package.json`, island build files) and `agentengine-server` (internal review draft) belongs to the user and must not be overwritten or committed.
- Every behavior change follows RED -> verify expected failure -> minimal GREEN -> focused regression -> commit.
- Before release, run the full Python suite through `uv run --extra all`; plain system `pytest` is not an accepted release result because it lacks the locked `fastmcp` environment.

---

### Task 1: Canonical Event Union, Deterministic Identity, and Reducer

**Files:**
- Create: `ksadk/events/canonical.py`
- Create: `ksadk/events/content.py`
- Create: `ksadk/events/identity.py`
- Create: `ksadk/events/reducer.py`
- Create: `ksadk_runtime_common/schemas/runtime_event_v2.json`
- Create: `tests/events/fixtures/runtime_event_v2.json`
- Create: `tests/events/test_canonical_runtime_event.py`
- Create: `tests/events/test_runtime_identity.py`
- Create: `tests/events/test_stream_reducer.py`

**Interfaces:**
- Produces: `RuntimeEvent`, `parse_runtime_event`, `dump_runtime_event`, `stable_scope_id`, `stable_item_id`, `stable_event_id`, `StreamReducer.apply`, `StreamReducer.snapshot`, `ProjectionPatch`, and `RunProjection`.
- Consumes: Pydantic 2 and JSON-compatible source metadata only.

- [ ] **Step 1: Write failing schema and identity tests**

```python
def test_identity_is_stable_across_reconstruction():
    assert stable_item_id("adk", "inv-1", "branch-a", "event-7") == (
        stable_item_id("adk", "inv-1", "branch-a", "event-7")
    )


def test_same_adk_response_uses_one_item_but_distinct_mutations():
    item_id = stable_item_id("adk", "inv-1", "branch-a", "event-7")
    delta_id = stable_event_id("adk", "scope-1", item_id, "item.updated", "text-0", "4", 0)
    done_id = stable_event_id("adk", "scope-1", item_id, "item.completed", "text-0", "5", 0)
    assert delta_id != done_id
```

Add literal fixtures for every canonical event family: run lifecycle, item lifecycle, interaction lifecycle, continuation lifecycle, context compaction, and usage. Validate them against both Pydantic and `runtime_event_v2.json`.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/test_canonical_runtime_event.py tests/events/test_runtime_identity.py -q`

Expected: collection fails because `ksadk.events.canonical` and `ksadk.events.identity` do not exist.

- [ ] **Step 3: Implement the discriminated union and stable identity encoding**

Use these public shapes exactly:

```python
class SourceRef(BaseModel):
    framework: Literal["adk", "langgraph", "codex", "a2a", "ksadk"]
    native_event_id: str | None = None
    native_cursor: str | None = None
    native_run_id: str | None = None
    native_item_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class EventEnvelope(BaseModel):
    schema_version: Literal[2] = 2
    event_id: str
    seq: int
    timestamp: float
    run_id: str
    run_seq: int | None = None
    scope_id: str
    parent_scope_id: str | None = None
    source: SourceRef
```

`RuntimeEvent` is `Annotated[Union[RunStarted, RunProgress, RunInterrupted, RunCompleted, RunFailed, RunCanceled, ItemStarted, ItemUpdated, ItemCompleted, ItemFailed, InteractionRequested, InteractionResolved, ContinuationCreated, ContinuationResumed, ContextCompactionStarted, ContextCompactionCompleted, UsageReported], Field(discriminator="event_type")]`.

`ItemUpdated.op` is `Literal["append", "replace"]`; `ItemCompleted.snapshot` is authoritative; `RunCompleted.output_refs` is the only final-output selector. Content values are typed `TextContent`, `JsonContent`, `ToolCallContent`, `ToolResultContent`, `ArtifactContent`, and `DataContent` objects with stable `part_id`.

Implement identity encoding as canonical JSON over normalized string components plus SHA-256, returning `<prefix>_<first 24 hex chars>`. Empty native identity components raise `ValueError` instead of falling back to text or UUID.

- [ ] **Step 4: Write failing reducer lifecycle tests**

```python
def test_completed_snapshot_replaces_provisional_text():
    reducer = StreamReducer()
    for event in (started("item-1"), appended("item-1", "hel"), completed("item-1", "hello")):
        reducer.apply(event)
    assert reducer.snapshot().items[0].parts[0].text == "hello"


def test_identical_text_in_distinct_items_is_not_deduplicated():
    reducer = StreamReducer()
    for event in complete_two_items_with_text("same"):
        reducer.apply(event)
    assert [item.parts[0].text for item in reducer.snapshot().items] == ["same", "same"]
```

Also test append-before-start, update-after-complete, incompatible item kind, run completion with open items, duplicate `event_id`, conflicting reused `seq`, output ref ordering, and a 1024-entry recent-event bound.

- [ ] **Step 5: Run reducer tests and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/test_stream_reducer.py -q`

Expected: collection fails because `StreamReducer` is missing.

- [ ] **Step 6: Implement the reducer and structured conformance errors**

```python
class StreamConformanceError(ValueError):
    code: str
    source: str
    scope_id: str
    item_id: str | None


class StreamReducer:
    RECENT_EVENT_LIMIT = 1024
```

Expose `apply(self, event: RuntimeEvent) -> ProjectionPatch` and
`snapshot(self) -> RunProjection`; keep mutation helpers private to this module.

The reducer keys items by `(scope_id, item_id)`, applies append/replace/complete to a named part, closes items exactly once, tracks `last_seq`, and uses a bounded LRU only for recent conflict diagnostics. It never inspects author or text similarity.

- [ ] **Step 7: Run Task 1 tests and commit**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/test_canonical_runtime_event.py tests/events/test_runtime_identity.py tests/events/test_stream_reducer.py -q`

Commit: `feat(events): add canonical runtime event reducer`

---

### Task 2: Read-Only RuntimeEvent v1 Compatibility Projection

**Files:**
- Create: `ksadk/events/v1_compat.py`
- Create: `tests/events/test_v1_compat.py`
- Modify: `tests/events/fixtures/runtime_event_v1.json`
- Modify: `ksadk_runtime_common/schemas/runtime_event_v1.json`

**Interfaces:**
- Consumes: canonical `RuntimeEvent`, `ProjectionPatch`, and `RunProjection` from Task 1.
- Produces: `RuntimeEventV1`, `RuntimeEventV1Parser`, `RuntimeEventV1ProjectionMode`, and `project_to_v1(event, mode="snapshot_only") -> tuple[RuntimeEventV1, ...]`.

- [ ] **Step 1: Write failing compatibility-mode tests**

```python
def test_snapshot_only_suppresses_delta_and_emits_one_terminal_snapshot():
    assert project_to_v1(item_text_append("hello"), mode="snapshot_only") == ()
    projected = project_to_v1(item_text_completed("hello"), mode="snapshot_only")
    assert [(event.event_type, event.payload["text"]) for event in projected] == [
        ("text.completed", "hello")
    ]


def test_identity_replace_completed_overwrites_instead_of_appending():
    parser = RuntimeEventV1Parser()
    for event in project_identity_replace_text("hel", "hello"):
        parser.feed(event)
    assert parser.transcript()["items"][0]["text"] == "hello"
```

Also assert that undeclared mode defaults to `snapshot_only`, `op=replace` is never emitted as an unannotated v1 delta, replaying the same v1 `event_id` is a no-op, and distinct v2 items with identical text remain distinct under `identity_replace`.

- [ ] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/test_v1_compat.py -q`

Expected: collection fails because `v1_compat.py` does not exist.

- [ ] **Step 3: Move the frozen v1 model/parser into the compatibility module**

Keep the v1 JSON envelope and required payload keys unchanged. The identity-aware parser key is `(invocation_id, scope_id, item_id, part_id, phase)` and uses `operation` to append or replace. Missing identity fields use the old `(invocation_id, phase)` key only for reading old wire data.

Implement `project_to_v1` as a stateless event projection: `snapshot_only` drops item content updates and emits only completed snapshots; `identity_replace` emits append/replace deltas plus completed snapshots with `scope_id`, `item_id`, `part_id`, `operation`, and `source_event_id`.

- [ ] **Step 4: Run compatibility fixtures and commit**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/test_v1_compat.py tests/events/test_runtime_event.py tests/events/test_runtime_event_deserialization.py -q`

Commit: `feat(events): isolate runtime event v1 projection`

---

### Task 3: Canonical Persistence, Replay, and Conformance Recovery

**Files:**
- Create: `ksadk/events/pipeline.py`
- Create: `ksadk/events/canonical_store.py`
- Create: `ksadk/events/canonical_replay.py`
- Modify: `tests/events/test_runtime_event_store.py`
- Create: `tests/events/test_runtime_event_recovery.py`
- Create: `tests/events/test_mixed_schema_replay.py`

**Interfaces:**
- Consumes: Task 1 canonical union/reducer and Task 2 read-only v1 types.
- Produces: public `RuntimeEvent`, `RuntimeEventStore`, `StreamReducer`, `replay_projection`, `list_legacy_session_events`, `LegacyRunNotResumableError`, and `CanonicalEventPipeline.ingest(event) -> tuple[RuntimeEvent, ...]`.

- [ ] **Step 1: Write failing store identity and cursor tests**

```python
@pytest.mark.asyncio
async def test_store_deduplicates_before_allocating_second_seq(store):
    first = await store.append_one(canonical_text_event(event_id="evt-1", seq=0))
    replay = await store.append_one(canonical_text_event(event_id="evt-1", seq=0))
    assert replay.seq == first.seq
    assert len(await store.list("session-1")) == 1


@pytest.mark.asyncio
async def test_same_event_id_in_different_sessions_is_allowed(two_session_store):
    left = await two_session_store.append_one(canonical_run_started("session-1", "evt-1"))
    right = await two_session_store.append_one(canonical_run_started("session-2", "evt-1"))
    assert left.event_id == right.event_id == "evt-1"
```

Test schema-v2-only writes, `(session_id, seq)` cursor ordering, persist-before-publish, live/full/cursor replay equality, and no unbounded in-memory event-id set.

- [ ] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/test_runtime_event_store.py tests/events/test_runtime_event_recovery.py -q`

Expected: failures show the current store serializes schema v1 payload envelopes and lacks canonical recovery.

- [ ] **Step 3: Implement schema-v2 storage and recovery pipeline**

Persist canonical JSON in the existing `SessionEvent` content/metadata carrier with `schema_version=2`; assign the session sequence in the session service before publishing. Reject schema-v1 writes. Use the session-scoped durable event id lookup/unique constraint and validate collisions against canonical serialized content.

`CanonicalEventPipeline.ingest` validates against a shadow reducer, persists valid canonical events, then applies/publishes them. Fatal source violations persist deterministic `item.failed` events for affected open items followed by `run.failed` carrying `ConformanceError`; a completed snapshot mismatch reconciles and increments a metric without failing the run.

- [ ] **Step 4: Write and verify mixed-schema tests**

```python
@pytest.mark.asyncio
async def test_legacy_view_keeps_old_run_and_projects_new_run_in_shared_seq_order(
    mixed_schema_session,
):
    events = await list_legacy_session_events("mixed-session")
    assert [event["Content"]["parts"][0]["text"] for event in assistant_events(events)] == [
        "old answer", "new answer"
    ]


@pytest.mark.asyncio
async def test_active_v1_run_cannot_resume_as_v2(mixed_schema_session):
    with pytest.raises(LegacyRunNotResumableError) as exc:
        await resume_legacy_run("run-v1")
    assert exc.value.status_code == 409
    assert exc.value.code == "legacy_run_not_resumable"
```

- [ ] **Step 5: Verify the canonical store remains isolated and commit**

Tasks 1-5 import canonical types from `ksadk.events.canonical` and `ksadk.events.canonical_store`; do not change package-level exports yet. Existing runtime paths remain green until the atomic public switch in Task 6. Confirm `canonical_store.py` accepts schema v2 only and never imports the v1 compatibility projector.

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events -q`

Commit: `feat(events): persist canonical runtime events`

---

### Task 4: Google ADK 2.6.3+ Identity Adapter and Duplicate Reproduction

**Files:**
- Create: `ksadk/events/adapters/__init__.py`
- Create: `ksadk/events/adapters/adk.py`
- Modify: `ksadk/runners/adk_runner.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/events/adapters/test_adk.py`
- Create: `tests/runners/test_adk_multi_llm_runtime_events.py`

**Interfaces:**
- Consumes: canonical constructors and identity functions from Task 1.
- Produces: `ADKEventAdapter.map(event, context) -> tuple[RuntimeEvent, ...]` and the not-yet-public `ADKRunner.stream_canonical_events(input_data)` integration seam used by Task 6.

- [ ] **Step 1: Write the exact multi-LLM failing fixture**

Create two ADK response lifecycles from the same subagent author. Each lifecycle has partial events and a full final event sharing its own `event.id`; the second LLM call uses a new `event.id`.

```python
async def test_same_subagent_two_llm_calls_emit_two_items_without_repeated_snapshots():
    events = [event async for event in runner.stream_canonical_events(input_data())]
    projection = reduce_events(events)
    assert visible_text_items(projection) == ["first answer", "second answer"]
    assert len({item.item_id for item in projection.items if item.kind == "message"}) == 2
```

Add a second fixture where both calls intentionally produce identical text; both items must remain.

- [ ] **Step 2: Run and verify RED against the current per-author heuristic**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/runners/test_adk_multi_llm_runtime_events.py -q`

Expected: the current `ADKRunner.stream` either duplicates a full snapshot or merges both calls under one author-level accumulator.

- [ ] **Step 3: Implement native ADK mapping**

Derive `scope_id` from KsADK invocation plus ADK branch/node path. Derive one `item_id` from `(invocation_id, branch path, event.id)`. Map partial text to `item.updated(op="append")`, the non-partial event with the same `event.id` to `item.completed(snapshot=...)`, and a new ADK event id to a new item. Preserve tool call/response ids as separate items linked by `call_id`. `is_final_response()` closes only that response item, not the whole run.

Delete `sub_agent_snapshots`, `sub_agent_thought_snapshots`, `sub_agent_last_output_kind`, and all prefix comparisons used for ADK output deduplication.

- [ ] **Step 4: Raise the ADK support floor and run compatibility tests**

Change the `adk` extra to `google-adk>=2.6.3,<3.0.0`, regenerate `uv.lock`, and run:

`UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/adapters/test_adk.py tests/runners/test_adk_multi_llm_runtime_events.py tests/test_runner.py -k 'adk or runtime_event' -q`

- [ ] **Step 5: Commit**

Commit: `feat(adk): preserve response identity across llm calls`

---

### Task 5: LangGraph, Codex, and A2A Native Identity Adapters

**Files:**
- Create: `ksadk/events/adapters/langgraph.py`
- Create: `ksadk/events/adapters/codex.py`
- Create: `ksadk/events/adapters/a2a.py`
- Create: `tests/events/adapters/test_langgraph.py`
- Create: `tests/events/adapters/test_codex.py`
- Create: `tests/events/adapters/test_a2a.py`

**Interfaces:**
- Produces: `LangGraphEventAdapter`, `CodexEventAdapter`, and `A2AEventAdapter` that all emit the Task 1 canonical union.
- Consumes: LangGraph stream events v3, Codex app-server v2 notifications, and A2A v1.0 Task/Message/Artifact updates.

- [ ] **Step 1: Write failing native-id fixtures**

For LangGraph, interleave two namespaces and assert `namespace + run/message/block identity` keeps their items separate. For Codex, assert delta loss is repaired by `item/completed` snapshot without duplication. For A2A, cover two artifacts, `append=true`, `append=false`, `lastChunk`, message-only responses, duplicate push, and terminal Task correction.

- [ ] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/adapters/test_langgraph.py tests/events/adapters/test_codex.py tests/events/adapters/test_a2a.py -q`

Expected: the adapter modules are missing and the current A2A adapter emits schema-v1 text events.

- [ ] **Step 3: Implement the three mappings**

- LangGraph: `scope_id = run_id + namespace`; message item uses native LLM run/message id; block uses native block id/index; source cursor remains provenance, while store assigns session seq.
- Codex: `scope_id = thread_id + turn_id`; retain native item and part ids; map started/delta/completed directly; keep reasoning, command, MCP tool, file change, and agent message as distinct kinds.
- A2A: `scope_id = context_id + task_id`; artifact item uses `task_id + artifact_id`; `append` controls append/replace and `lastChunk` only controls completion; message item uses `message_id`.

When A2A lacks extension occurrence ids, mark live updates provisional. On terminal/reconnect/subscription rebuild, call `GetTask`, replace artifact projections from the authoritative snapshot, and do not mark replay consistent if `GetTask` fails.

- [ ] **Step 4: Run adapter and existing framework suites**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events/adapters tests/runners/test_codex_runtime_adapter.py tests/a2a -q`

- [ ] **Step 5: Commit**

Commit: `feat(runtime): add native identity adapters`

---

### Task 6: Switch RuntimeAdapter, Store, Replay, and Final Output to Canonical Events

**Files:**
- Modify: `ksadk/events/__init__.py`
- Modify: `ksadk/events/runtime_event.py`
- Modify: `ksadk/events/store.py`
- Modify: `ksadk/events/replay.py`
- Delete: `ksadk/events/parser.py`
- Modify: `ksadk/runtime/adapter.py`
- Modify: `ksadk/runtime/runner_adapter.py`
- Modify: `ksadk/runtime/framework_adapters.py`
- Modify: `ksadk/runners/langgraph_runner.py`
- Modify: `ksadk/codex/runtime.py`
- Modify: `ksadk/runtime/conversation_execution.py`
- Modify: `ksadk/conversations/runtime_streaming.py`
- Modify: `ksadk/conversations/runtime_stream_events.py`
- Modify: `ksadk/conversations/message_projection.py`
- Modify: `ksadk/server/routes/projection.py`
- Modify: `ksadk/cli/cmd_replay.py`
- Modify: `tests/runtime/test_responses_streaming.py`
- Modify: `tests/test_conversation_runtime.py`
- Modify: `tests/test_server_session_app.py`

**Interfaces:**
- Consumes: canonical source adapters, `RuntimeEventStore`, `StreamReducer`, and `project_to_v1`.
- Produces: canonical `RuntimeAdapter.stream`, canonical replay, `run.completed.output_refs` final selection, standard Responses SSE, and legacy SessionEvent projections with `Metadata.RuntimeItem`.

- [ ] **Step 1: Write failing runtime integration tests**

```python
@pytest.mark.asyncio
async def test_runtime_live_and_replay_use_identical_projection(runtime_fixture):
    live = await collect_live_projection(runtime_fixture)
    replay = await replay_projection(runtime_fixture.store, runtime_fixture.session_id)
    assert replay.model_dump() == live.model_dump()


@pytest.mark.asyncio
async def test_final_output_uses_only_run_completed_output_refs(runtime_fixture):
    response = await invoke_runtime_conversation_once(**runtime_fixture.kwargs)
    assert response["output_text"] == "selected answer"
    assert "commentary" not in response["output_text"]
```

Add cursor reconnect, duplicate event id, multiple identical items, mixed schema legacy read view, `Metadata.RuntimeItem`, and the explicit 409 legacy-run resume case.

- [ ] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/runtime/test_responses_streaming.py tests/test_conversation_runtime.py -k 'runtime_event or live_and_replay or output_refs or legacy_run' -q`

Expected: current runtime code expects v1 `payload`, `phase`, and `EventType.TEXT_*` fields.

- [ ] **Step 3: Switch the runtime pipeline**

All `RuntimeAdapter.stream` implementations now return canonical events. Persist each event before applying/publishing it. Build live and replay from the same `StreamReducer`. Choose final response strictly from `RunCompleted.output_refs`.

Responses serialization consumes `ProjectionPatch`: append emits `response.output_text.delta`; replace emits an identity-addressed content update or a terminal authoritative output item; completed closes the output item without re-appending its full snapshot. Remove all text prefix reconciliation from `conversation_execution.py`.

Session projection keeps existing public fields and adds:

```json
{"RuntimeItem":{"RunId":"run-1","ScopeId":"scope-1","ItemId":"item-1","PartId":"text-0","Operation":"replace","SourceEventId":"native-1"}}
```

Legacy checkpoint filters must require `continuation_kind=graph_checkpoint` before pagination and `Total` calculation.

- [ ] **Step 4: Remove the old internal v1 modules and imports**

Switch `ksadk.events.RuntimeEvent` and `ksadk.events.RuntimeEventStore` to the canonical implementations and export `replay_projection`. Delete `ksadk/events/parser.py`; replace the old schema-v1 implementations in `ksadk/events/runtime_event.py`, `ksadk/events/store.py`, and `ksadk/events/replay.py` with canonical re-exports/delegation. V1 types/parser remain only in `v1_compat.py`. Ensure no production source imports `EventType` v1 outside that module or dedicated v1 wire tests.

- [ ] **Step 5: Run Python runtime integration tests and commit**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/events tests/runtime tests/agui tests/a2a tests/runners/test_codex_runtime_adapter.py tests/test_conversation_runtime.py tests/test_server_session_app.py -q`

Commit: `refactor(runtime): make canonical events the single fact source`

---

### Task 7: Standard Protocol and Studio Backend Projections

**Files:**
- Modify: `ksadk/agui/agent.py`
- Modify: `ksadk/agui/a2ui_projection.py`
- Modify: `ksadk/a2a/event_adapter.py`
- Modify: `ksadk/a2a/executor.py`
- Modify: `ksadk/a2a/space_client.py`
- Modify: `ksadk/studio/run_service.py`
- Modify: `tests/agui/test_runtime_adapter.py`
- Modify: `tests/a2a/test_a2a_protocol_e2e.py`
- Modify: `tests/studio/test_run_service.py`
- Create: `tests/protocol/test_cross_projection_golden.py`
- Create: `tests/events/fixtures/runtime_projection_golden.json`

**Interfaces:**
- Consumes: canonical `ProjectionPatch`/`RunProjection` only.
- Produces: AG-UI events, A2UI surfaces/actions, A2A Task/artifact updates, and Studio `message/thinking/tool/interaction` item-aware events.

- [ ] **Step 1: Write failing cross-protocol golden tests**

Use one literal canonical fixture containing two text items, identical legal text in two ids, reasoning, tool call/result, approval, structured input, A2UI surface/action, and terminal output refs. Assert each protocol keeps the same item ordering and terminal answer without duplicate text.

- [ ] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/protocol/test_cross_projection_golden.py -q`

Expected: existing projectors accept schema-v1 event types and use accumulated prefix state.

- [ ] **Step 3: Replace direct v1 consumers**

- AG-UI emits message start/content/end per canonical item id; replace addresses the same item and never trims text prefixes.
- A2UI remains a data-item projection; approval and structured input remain interaction events rather than being folded into A2UI.
- A2A artifact projection uses canonical artifact identity and operation; terminal closure invokes the Task snapshot reconciliation rule.
- Studio backend includes `runId`, `scopeId`, `itemId`, `partId`, and `operation` in `message.delta`, `message.completed`, `thinking.*`, command/tool, and interaction event data.

- [ ] **Step 4: Run protocol suites and commit**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/protocol tests/agui tests/a2a tests/studio/test_run_service.py -q`

Commit: `refactor(protocols): project canonical runtime items`

---

### Task 8: ksadk-web Identity-Aware Stream and Session Reducer

**Files:**
- Create: `../ksadk-web/src/core/stream/runtime-items.ts`
- Create: `../ksadk-web/src/__tests__/runtime-items.test.ts`
- Modify: `../ksadk-web/src/utils/responses-stream.js`
- Modify: `../ksadk-web/src/utils/session-events.js`
- Modify: `../ksadk-web/src/core/stream/responses-protocol.ts`
- Modify: `../ksadk-web/src/stores/streaming.ts`
- Modify: `../ksadk-web/src/components/chat/types.ts`
- Modify: `../ksadk-web/tests/responses-stream.test.mjs`
- Modify: `../ksadk-web/tests/session-events.test.mjs`

**Interfaces:**
- Consumes: Responses item ids, `Metadata.RuntimeItem`, and deterministic legacy server events.
- Produces: `RuntimeItemReducer.apply`, `RuntimeItemReducer.snapshot`, and message projections keyed by `runId/scopeId/itemId/partId`.

- [ ] **Step 1: Write failing reducer tests**

```typescript
it('replaces a completed snapshot without appending it twice', () => {
  const reducer = new RuntimeItemReducer();
  reducer.apply(start('item-1'));
  reducer.apply(append('item-1', 'hel'));
  reducer.apply(complete('item-1', 'hello'));
  expect(reducer.snapshot().items[0].parts[0].text).toBe('hello');
});

it('preserves identical text from distinct item ids', () => {
  expect(project(twoCompletedItems('same')).map(item => item.text)).toEqual(['same', 'same']);
});
```

Also cover event-id replay, interleaved subagents, session switch, refresh replay, cursor reconnect, Responses output item ids, and legacy `assistant_stream_snapshot -> replace` mapping.

- [ ] **Step 2: Run and verify RED**

Run: `cd ../ksadk-web && npm test -- src/__tests__/runtime-items.test.ts`

Expected: `RuntimeItemReducer` does not exist.

- [ ] **Step 3: Implement the single Web reducer and ingress adapters**

The core store has no `lastText`, per-agent accumulator, `startswith`, suffix overlap, or text hash. Responses, identity-aware session events, and AG-UI actions normalize into the same item operations.

The only synthesized identity path is legacy server ingress:

```typescript
const scopeId = invocationId;
const itemId = `${invocationId}:legacy-assistant`;
```

Map `assistant_stream_snapshot` to replace and `assistant_message` to complete. Do not infer identity from the previous message or body text.

- [ ] **Step 4: Run Web tests/build and commit in the ksadk-web repository**

Run: `cd ../ksadk-web && npm test && npm run build:ksadk && npm run build:hosted`

Commit: `fix(stream): reduce output by runtime item identity`

---

### Task 9: React Studio, Embedded Agent UI, and Hosted UI Same-Version Gate

**Files:**
- Modify: `ksadk/studio/react-ui/src/chatProtocol.ts`
- Modify: `ksadk/studio/react-ui/src/chatProtocol.test.mjs`
- Modify: `ksadk/studio/react-ui/src/pages/ObservabilityPage.tsx`
- Modify: `tests/studio/test_style_system.py`
- Modify: `Makefile`
- Modify: `tests/test_runtime_common_packaging.py`
- Modify: `../agentengine-hosted-ui/package.json`
- Modify: `../agentengine-hosted-ui/package-lock.json`
- Modify: `../agentengine-hosted-ui/tests/ksadk-web-dependency.test.mjs`
- Modify: `../agentengine-hosted-ui/tests/shared-ui-shell.test.mjs`

**Interfaces:**
- Consumes: Studio item-aware events from Task 7 and the published ksadk-web package from Task 8.
- Produces: item-aware Studio activities, a synced embedded Agent UI bundle, and a thin Hosted UI shell pinned to the same ksadk-web release.

- [ ] **Step 1: Write failing React Studio item tests**

```javascript
test('projects two completed message items without replacing the first item', () => {
  const projection = projectRunActivities(twoMessageItemsFixture);
  assert.deepEqual(projection.textItems.map(item => item.text), ['first', 'second']);
});
```

Add replace/completed, identical-text items, refresh replay, and Observability content-card ordering tests.

- [ ] **Step 2: Run and verify RED**

Run: `cd ksadk/studio/react-ui && npm test`

Expected: current `projectRunActivities` stores one run-wide `output` string and replaces the first item.

- [ ] **Step 3: Implement Studio item indexing and build it**

Extend `RunActivityProjection` with `textItems: RuntimeTextItem[]`, where each item contains `runId`, `scopeId`, `itemId`, `partId`, `phase`, `text`, and `completed`. Index Studio text/reasoning activities by `runId/scopeId/itemId/partId`; apply append/replace/complete explicitly, and derive the existing aggregate `output` only from terminal output refs. Keep command/tool/approval activity ids stable. Reuse the cross-language golden fixture through a JSON test fixture rather than copying reducer logic.

Run: `cd ksadk/studio/react-ui && npm test && npm run build`

- [ ] **Step 4: Sync and verify the embedded Agent UI**

Use the existing `make sync-ksadk-web-static` flow against the Task 8 package/build output. Do not hand-edit `ksadk/server/static` or commit `dist-*` directories.

Run: `make sync-ksadk-web-static && UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/test_runtime_common_packaging.py tests/test_unified_agent_ui_local.py -q`

- [ ] **Step 5: Upgrade and verify the thin Hosted UI shell**

Pin `@kingsoftcloud/ksadk-web` to the Task 8 published/release-candidate version in both manifest and lockfile. The Hosted UI must continue to import `AgentWorkbench` and must not add a local stream/session parser.

Run: `cd ../agentengine-hosted-ui && npm test && npm run build:hosted`

- [ ] **Step 6: Commit per repository**

Commit in ksadk-python: `fix(studio): project runtime items by identity`

Commit in agentengine-hosted-ui: `chore(web): consume identity-aware ksadk web`

---

### Task 10: AgentEngine Contracts, Release Documentation, and 0.8.1 Verification

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/public-release-workflow.md`
- Modify: `tests/test_public_release_positioning.py`
- Create: `tests/release/test_runtime_event_v2_release_gate.py`
- Read/test only: `../agentengine-server/app/api/v1/actions/chat_actions.py`
- Read/test only: `../agentengine-server/app/services/conversation_runtime_service.py`
- Read/test only: `../agentengine-server/app/services/chat_service.py`

**Interfaces:**
- Consumes: all prior task commits and their test evidence.
- Produces: release notes, capability documentation, an executable release gate, and a verified 0.8.1 candidate.

- [ ] **Step 1: Write failing release contract tests**

The release gate must exercise behavior rather than grep implementation text:

- stream a standard Responses fixture through AgentEngine `_update_stream_text` and assert correct accumulated text;
- pass a raw `text.completed` fixture and assert it is not treated as a Responses output delta;
- verify `RunAgent(Stream=true)` remains `text/event-stream` with unchanged envelope fields;
- verify `ListSessionEvents` fields remain stable with additive `Metadata.RuntimeItem`;
- verify `EventTypes=["run_checkpoint"]` includes only graph checkpoints and has exact `Total`/pagination;
- verify mixed-schema old/new runs remain visible in legacy session views;
- verify active v1 resume returns 409.

- [ ] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest tests/release/test_runtime_event_v2_release_gate.py -q`

Expected: documentation/capability assertions or missing fixtures fail until the release surface is updated.

- [ ] **Step 3: Update capability and release documentation**

Document `RuntimeEventVersions=[1,2]`, `RuntimeEventDefault=2`, `RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]`, and `RuntimeEventV1ProjectionDefault="snapshot_only"`. Replace the 0.8.1 changelog statement that RuntimeEvent remains additive v1 with the canonical-v2/read-only-v1 boundary and list the synchronized Web/Studio/Hosted UI requirement.

- [ ] **Step 4: Run focused cross-repository gates**

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest \
  tests/events tests/runtime tests/protocol tests/agui tests/a2a tests/studio \
  tests/test_conversation_runtime.py tests/test_server_session_app.py \
  tests/release/test_runtime_event_v2_release_gate.py -q

cd ../ksadk-web && npm test && npm run build:ksadk && npm run build:hosted
cd ../ksadk-python/ksadk/studio/react-ui && npm test && npm run build
cd ../../../../agentengine-hosted-ui && npm test && npm run build:hosted
```

- [ ] **Step 5: Run full release gates**

From ksadk-python:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run --extra all pytest -q
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run ruff check ksadk tests
UV_CACHE_DIR=/private/tmp/uv-cache-ksadk-runtime-v2 uv run mypy ksadk/events ksadk/runtime
make public-build-check
```

Run AgentEngine server's existing RunAgent/session/checkpoint contract suites without changing server production code. If a server test needs a new fixture, keep it outside this release branch or report it as an external contract artifact; do not commit over the server team's `test` branch work.

- [ ] **Step 6: Perform real smoke tests where credentials/runtime are available**

Run ADK 2.6.3+ two-LLM-call reproduction, LangGraph v3 stream, Codex app-server item lifecycle, A2A append/replace/GetTask reconciliation, local embedded Agent UI RunAgent -> refresh -> replay, and pre-production Hosted UI RunAgent -> reconnect -> resume/cancel. Record unavailable external smokes explicitly; do not describe them as passing.

- [ ] **Step 7: Commit release documentation and produce the candidate report**

Commit: `docs(release): finalize runtime event v2 candidate`

The candidate report lists every command, result, skipped external smoke, repository commit, package version, and known non-blocking warning. Do not publish PyPI, npm, images, tags, or GitHub releases without the maintainer's explicit release approval.
