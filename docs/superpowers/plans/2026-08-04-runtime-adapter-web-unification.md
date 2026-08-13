# RuntimeAdapter 核心与统一 Web 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 让 `RuntimeAdapter` 和核心 `RuntimeRegistry` 成为 Web、Studio、Responses、AG-UI、A2A 与 Harness 唯一消费的执行合同；Codex 只公开 `CodexRuntimeAdapter`，并删除 Codex/Studio 的重复运行层。

**架构：** 在核心层增加 `RuntimeLaunchContext` 和基于 Factory 的 `RuntimeRegistry`，由 `RuntimeExecutor` 统一路由生命周期。Codex 直接构造 `CodexRuntimeAdapter`；ADK 和 LangGraph 通过现有 Adapter 继续使用内部 Runner。协议层与产品层只接收 Adapter/Executor，禁止导入 `ksadk.runners`。

**技术栈：** Python 3.10+、asyncio、FastAPI、Pydantic v2、pytest、Ruff、OpenTelemetry RuntimeEvent 合同。

## 本阶段达到的效果

1. Codex、ADK、LangGraph 都通过同一个 Factory Registry 创建 `RuntimeAdapter`，上层不再识别具体 Runner。
2. Web、Studio、OpenAI Responses、AG-UI、A2A、Harness 与生成启动代码全部通过 `RuntimeExecutor` 执行。
3. Codex 的唯一公开实现名为 `CodexRuntimeAdapter`；重复的 Codex/Studio Runtime 层和 `CodexRunner` 被删除。
4. `BaseRunner` 只保留框架内部加载、原生流式调用、能力声明和关闭职责，不再负责 Web、会话或平台生命周期。
5. 架构测试、三种 Runtime 合同测试、协议测试、Studio 测试和全量测试共同证明执行语义没有分叉。

## 本阶段停止条件

只有 Task 11 的全部门禁通过，且待删除符号和禁止导入均为零引用，才允许结束本实施计划。达到该条件也**不代表总 Goal 完成**；必须继续执行后续五份计划，直至创建、Runtime Catalog、全局上下文、Tool/Skill、会话恢复、多模型、OTEL Trace 和应用内浏览器验收全部满足总设计文档的 Goal 停止条件。

不得因为时间、token、工作量、测试失败或改动文件较多而提前停止；失败必须回到所属 Task，补充回归测试后继续修复。

## 全局约束

- 冻结的六个 `RuntimeAdapter` 动词及其签名保持不变：`start`、`stream`、`cancel`、`resume`、`checkpoint`、`close`。
- `RuntimeEvent` 继续作为唯一实时事件合同。
- Codex 公开执行类统一命名为 `CodexRuntimeAdapter`，不得保留名为 `CodexRuntime` 的兼容别名。
- 调用方迁移后删除 `CodexRunner`、`CodexStudioRuntime`、`StudioCodexRuntimeAdapter`、`StudioRuntimeAdapterRegistry` 和 `StudioRuntimeOrchestrator`。
- Web、Studio、Responses、AG-UI、A2A 与 Harness 不得直接导入 `ksadk.runners`。
- `BaseRunner` 仅作为 ADK、LangGraph、LangChain、DeepAgents、Remote 和自定义 Runner 的内部兼容层。
- 本计划不得修改 RuntimeEvent 名称、OpenAI Responses 线格式、OTLP 字段名或云 API 合同。
- 保留工作树内所有无关改动；每个提交只暂存任务声明的文件。
- 每项行为变更必须测试先行。

---

## 文件职责图

### 核心 Runtime

- `ksadk/runtime/adapter.py`：冻结的 Adapter 动词和唯一的 Factory Registry。
- `ksadk/runtime/launch.py`：传给 Adapter Factory 的不可变启动上下文与依赖服务。
- `ksadk/runtime/executor.py`：Adapter/Handle 所有权与生命周期路由，不包含 Studio 持久化职责。
- `ksadk/runtime/factory.py`：Codex、ADK、LangGraph 与旧框架的默认 Adapter Factory。
- `ksadk/runtime/framework_adapters.py`：ADK/LangGraph 专属 resume/checkpoint 语义。
- `ksadk/runtime/runner_adapter.py`：内部 BaseRunner 到 RuntimeAdapter 的桥。
- `ksadk/runtime/__init__.py`：Runtime 公共导出。

### Codex

- `ksadk/codex/runtime.py`：`CodexRuntimeAdapter`，包含标准事件和直接会话预处理。
- `ksadk/codex/client.py`：Codex 传输和可选星流 Proxy 注入。
- 删除 `ksadk/runners/codex_runner.py`。

### Runtime 消费方

- `ksadk/server/factory.py`：围绕 RuntimeExecutor/RuntimeAdapter 组装 FastAPI。
- `ksadk/server/composition.py`：不依赖 Runner 的路由组装。
- `ksadk/cli/cmd_web.py`：检测项目、构造 Adapter、启动统一 Web。
- `ksadk/cli/cmd_run.py`：Server 模式复用同一 Adapter Factory。
- `ksadk/cli/cmd_a2a.py`：A2A 使用标准 Registry。
- `ksadk/agui/routes.py`：接收 RuntimeAdapter，不再自行选择 Runner 包装器。
- `ksadk/harness/app.py`：构造 Adapter 并传入 App Factory。
- `ksadk/builders/code_builder.py`、`ksadk/builders/container_builder.py`、`ksadk/deployment/manager.py`：生成的启动代码使用 Adapter Factory。

### Studio

- `ksadk/studio/service.py`：解析 Build/Manifest、创建启动上下文、调用核心 RuntimeExecutor。
- `ksadk/studio/shared_web.py`：只投影核心 RuntimeEvent/Run 记录。
- `ksadk/studio/api.py`：Responses 和 Studio 路由调用公共服务。
- 删除 `ksadk/studio/codex_runtime.py`。
- 删除 `ksadk/studio/runtime_orchestrator.py`。

### 测试

- `tests/architecture/test_runtime_boundaries.py`：禁止导入和待删除符号必须零引用。
- `tests/runtime/test_registry.py`：Factory Registry 合同。
- `tests/runtime/test_executor.py`：生命周期和 Handle 所有权合同。
- `tests/runners/test_codex_runtime_adapter.py`：Codex 直连 Adapter 合同。
- 既有 Server、CLI、A2A、AG-UI、Harness 和 Studio 测试从 Runner Fake 迁移到 RuntimeAdapter Fake。

---

### Task 1：用失败测试冻结架构边界

**Files:**
- Create: `tests/architecture/test_runtime_boundaries.py`
- Modify: `tests/runners/test_adapter_interface.py`

**Interfaces:**
- Consumes: current repository source tree.
- Produces: executable rules that later tasks must satisfy.

- [ ] **Step 1: Write the deleted-symbol and forbidden-import tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_CONSUMERS = ("server", "studio", "agui", "a2a")
DELETED_SYMBOLS = (
    "CodexRunner",
    "CodexStudioRuntime",
    "StudioCodexRuntimeAdapter",
    "StudioRuntimeAdapterRegistry",
    "StudioRuntimeOrchestrator",
)


def test_product_and_protocol_layers_do_not_import_runners() -> None:
    violations = []
    for package in FORBIDDEN_CONSUMERS:
        for path in (ROOT / "ksadk" / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "ksadk.runners" in text:
                violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_removed_runtime_layers_have_no_source_references() -> None:
    violations = []
    for path in (ROOT / "ksadk").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for symbol in DELETED_SYMBOLS:
            if symbol in text:
                violations.append(f"{path.relative_to(ROOT)}:{symbol}")
    assert violations == []
```

- [ ] **Step 2: Add the canonical Codex class-name assertion**

```python
def test_codex_runtime_adapter_is_the_only_public_codex_adapter() -> None:
    from ksadk.codex.runtime import CodexRuntimeAdapter

    assert issubclass(CodexRuntimeAdapter, RuntimeAdapter)
    assert not hasattr(import_module("ksadk.codex.runtime"), "CodexRuntime")
```

- [ ] **Step 3: Run the tests and verify the intended failures**

Run:

```bash
uv run pytest tests/architecture/test_runtime_boundaries.py tests/runners/test_adapter_interface.py -q
```

Expected: failures list current Studio/AG-UI/A2A runner imports, deleted symbols, and missing `CodexRuntimeAdapter`.

- [ ] **Step 4: Commit only the architectural tests**

```bash
git add tests/architecture/test_runtime_boundaries.py tests/runners/test_adapter_interface.py
git commit -m "test(runtime): freeze unified adapter boundaries"
```

### Task 2：统一 Codex Adapter 命名并迁移 Runner 专属预处理

**Files:**
- Modify: `ksadk/codex/runtime.py`
- Modify: `ksadk/codex/__init__.py`
- Rename: `tests/runners/test_codex_runtime.py` → `tests/runners/test_codex_runtime_adapter.py`
- Modify: `tests/codex/test_proxy_injection.py`

**Interfaces:**
- Consumes: frozen `RuntimeAdapter`, `StartRequest`, `RuntimeEvent`.
- Produces: `CodexRuntimeAdapter(client, sandbox_read_only=True, turn_timeout_seconds=None)`.

- [ ] **Step 1: Change tests to import only `CodexRuntimeAdapter`**

```python
from ksadk.codex.runtime import CodexRuntimeAdapter


def test_codex_adapter_consumes_canonical_conversation_messages(fake_client):
    adapter = CodexRuntimeAdapter(fake_client)
    request = StartRequest(
        input="current",
        user_id="u1",
        session_id="s1",
        metadata={
            CONVERSATION_PREPROCESSING_METADATA_KEY: {
                "messages": [
                    {"role": "user", "content": "previous"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "current"},
                ]
            }
        },
    )
    handle = await adapter.start(request)
    await collect(adapter.stream(handle))
    assert "previous" in fake_client.last_prompt
    assert fake_client.last_prompt.endswith("current")
```

- [ ] **Step 2: Run the focused test and verify it fails because the class is missing**

Run:

```bash
uv run pytest tests/runners/test_codex_runtime_adapter.py -q
```

Expected: import failure for `CodexRuntimeAdapter`.

- [ ] **Step 3: Rename the class without a compatibility alias**

```python
class CodexRuntimeAdapter(RuntimeAdapter):
    """Codex native RuntimeAdapter backed directly by the Codex client."""
```

Update `__all__` to export only `CodexRuntimeAdapter` for the execution class.

- [ ] **Step 4: Add canonical conversation rendering inside the adapter**

```python
def _request_prompt(request: StartRequest) -> str:
    payload = request.metadata.get(CONVERSATION_PREPROCESSING_METADATA_KEY)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return request.input
    rendered = render_conversation_messages(messages)
    return rendered or request.input
```

Use `_request_prompt(request)` when starting the turn. Keep model, base instructions and cwd in `StartRequest`.

- [ ] **Step 5: Preserve direct RuntimeEvent and proxy telemetry behavior**

Add tests that collect `TEXT_DELTA`, `TEXT_COMPLETED`, `TOOL_CALL_BEGIN`, `TOOL_CALL_END`, `USAGE_REPORTED`, terminal and proxy progress events directly from the adapter. Do not introduce dict chunks.

- [ ] **Step 6: Run Codex adapter and proxy tests**

Run:

```bash
uv run pytest tests/runners/test_codex_runtime_adapter.py tests/codex/test_proxy_injection.py -q
```

Expected: all tests pass and `rg "class CodexRuntime" ksadk/codex` returns no match.

- [ ] **Step 7: Commit the native adapter rename**

```bash
git add ksadk/codex/runtime.py ksadk/codex/__init__.py tests/runners/test_codex_runtime_adapter.py tests/codex/test_proxy_injection.py
git add -u tests/runners/test_codex_runtime.py
git commit -m "refactor(codex): expose native runtime adapter"
```

### Task 3：将核心 RuntimeRegistry 改为 Factory 驱动

**Files:**
- Create: `ksadk/runtime/launch.py`
- Modify: `ksadk/runtime/adapter.py`
- Modify: `ksadk/runtime/__init__.py`
- Create: `tests/runtime/test_registry.py`

**Interfaces:**
- Consumes: RuntimeAdapter implementations.
- Produces: `RuntimeLaunchContext`, `RuntimeServices`, `RuntimeAdapterFactory`, `RuntimeRegistry.register`, `RuntimeRegistry.create`.

- [ ] **Step 1: Write registry factory tests**

```python
def test_registry_builds_adapter_from_launch_context(tmp_path):
    contexts = []

    def factory(context: RuntimeLaunchContext) -> RuntimeAdapter:
        contexts.append(context)
        return RecordingAdapter()

    registry = RuntimeRegistry()
    registry.register("fixture", factory)
    context = RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path)
    adapter = registry.create(context)

    assert isinstance(adapter, RecordingAdapter)
    assert contexts == [context]
    assert registry.registered_types() == ["fixture"]
```

Also cover blank names, duplicate registration, missing type and factories returning non-adapters.

- [ ] **Step 2: Run the registry tests and verify the missing contract failure**

Run:

```bash
uv run pytest tests/runtime/test_registry.py -q
```

Expected: import failures for `RuntimeLaunchContext` and factory-backed `create`.

- [ ] **Step 3: Implement launch contracts**

```python
@dataclass(frozen=True)
class RuntimeServices:
    codex_client_factory: Callable[[], Any] | None = None
    runner_factory: Callable[[Any, str], Any] | None = None


@dataclass(frozen=True)
class RuntimeLaunchContext:
    runtime_type: str
    project_dir: Path
    detection: Any | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    services: RuntimeServices = field(default_factory=RuntimeServices)
```

- [ ] **Step 4: Change RuntimeRegistry to store one factory map**

```python
RuntimeAdapterFactory = Callable[[RuntimeLaunchContext], RuntimeAdapter]


class RuntimeRegistry:
    def register(self, runtime_type: str, factory: RuntimeAdapterFactory) -> None:
        key = runtime_type.strip().lower()
        if not key or key in self._factories:
            raise ValueError(f"invalid or duplicate runtime type: {runtime_type!r}")
        self._factories[key] = factory

    def create(self, context: RuntimeLaunchContext) -> RuntimeAdapter:
        factory = self._factories[context.runtime_type.strip().lower()]
        adapter = factory(context)
        if not isinstance(adapter, RuntimeAdapter):
            raise TypeError("runtime factory must return RuntimeAdapter")
        return adapter

    def registered_types(self) -> list[str]:
        return sorted(self._factories)
```

Reject duplicate keys instead of silently overwriting them.

- [ ] **Step 5: Run registry and adapter-interface tests**

Run:

```bash
uv run pytest tests/runtime/test_registry.py tests/runners/test_adapter_interface.py -q
```

Expected: pass.

- [ ] **Step 6: Commit the core registry**

```bash
git add ksadk/runtime/launch.py ksadk/runtime/adapter.py ksadk/runtime/__init__.py tests/runtime/test_registry.py
git commit -m "refactor(runtime): centralize adapter factories"
```

### Task 4：增加核心 RuntimeExecutor 与 Handle 所有权

**Files:**
- Create: `ksadk/runtime/executor.py`
- Modify: `ksadk/runtime/__init__.py`
- Create: `tests/runtime/test_executor.py`

**Interfaces:**
- Consumes: `RuntimeRegistry`, `RuntimeLaunchContext`, `StartRequest`, frozen RuntimeAdapter verbs.
- Produces: `RuntimeExecutor.start`, `stream`, `cancel`, `resume`, `checkpoint`, `close`, `attach`.

- [ ] **Step 1: Write lifecycle-routing tests**

```python
async def test_executor_routes_handle_to_owning_adapter(registry, launch_context):
    executor = RuntimeExecutor(registry)
    handle = await executor.start(launch_context, start_request())

    events = [event async for event in executor.stream(handle)]
    result = await executor.cancel(handle)
    await executor.close(handle)

    assert [event.event_type for event in events] == [EventType.RUN_STARTED]
    assert result == CancelResult.NOT_RUNNING
    assert executor.is_attached(handle) is False
```

Add tests for unknown handles, launch-type mismatch, attach through durable adapter, resume and close after exceptions.

- [ ] **Step 2: Run executor tests and verify missing-class failures**

Run:

```bash
uv run pytest tests/runtime/test_executor.py -q
```

Expected: import failure for `RuntimeExecutor`.

- [ ] **Step 3: Implement per-handle adapter ownership**

```python
class RuntimeExecutor:
    def __init__(self, registry: RuntimeRegistry) -> None:
        self._registry = registry
        self._adapters: dict[tuple[str, str], RuntimeAdapter] = {}

    async def start(self, context, request):
        adapter = self._registry.create(context)
        handle = await adapter.start(request)
        self._adapters[(handle.runtime_type, handle.run_id)] = adapter
        return handle
```

All remaining verbs resolve the owning adapter by `(runtime_type, run_id)`. `close` removes ownership in `finally`.

- [ ] **Step 4: Implement explicit attach**

`attach(context, handle)` creates the declared adapter, calls `adapter.attach(handle)`, verifies returned identity and records ownership. It must not treat deserialized handle data as proof of a live runtime.

- [ ] **Step 5: Run executor tests including cancellation and attach**

Run:

```bash
uv run pytest tests/runtime/test_executor.py tests/runners/test_runtime_resume_cancel_e2e.py -q
```

Expected: pass.

- [ ] **Step 6: Commit RuntimeExecutor**

```bash
git add ksadk/runtime/executor.py ksadk/runtime/__init__.py tests/runtime/test_executor.py
git commit -m "feat(runtime): add shared lifecycle executor"
```

### Task 5：在唯一默认 Registry 中注册 Codex、ADK、LangGraph

**Files:**
- Create: `ksadk/runtime/factory.py`
- Modify: `ksadk/runtime/framework_adapters.py`
- Modify: `ksadk/runners/factory.py`
- Create: `tests/runtime/test_default_factory.py`

**Interfaces:**
- Consumes: `RuntimeLaunchContext`, `CodexRuntimeAdapter`, `ADKRuntimeAdapter`, `LangGraphRuntimeAdapter`.
- Produces: `build_default_runtime_registry()`, `create_runtime_adapter(context)`.

- [ ] **Step 1: Write the three-runtime factory test**

```python
@pytest.mark.parametrize(
    ("runtime_type", "adapter_type"),
    [
        ("codex", CodexRuntimeAdapter),
        ("adk", ADKRuntimeAdapter),
        ("langgraph", LangGraphRuntimeAdapter),
    ],
)
def test_default_registry_creates_expected_adapter(runtime_type, adapter_type, contexts):
    adapter = create_runtime_adapter(contexts[runtime_type])
    assert isinstance(adapter, adapter_type)
```

Use injected client/runner factories so the test does not contact a model provider.

- [ ] **Step 2: Run and verify the factory API is missing**

Run:

```bash
uv run pytest tests/runtime/test_default_factory.py -q
```

Expected: import failure for `create_runtime_adapter`.

- [ ] **Step 3: Implement Codex direct factory**

```python
def _create_codex(context: RuntimeLaunchContext) -> RuntimeAdapter:
    factory = context.services.codex_client_factory or AsyncCodexClient
    client = factory()
    return CodexRuntimeAdapter(client, sandbox_read_only=True)
```

Map YAML prompt/model/cwd into launch config and later `StartRequest`; do not construct `CodexRunner`.

- [ ] **Step 4: Implement ADK and LangGraph factories**

Create their existing Runner inside `ksadk/runtime/factory.py`, then pass it to the corresponding Adapter. `ksadk/runners/factory.py` remains an internal helper for legacy/custom runners and must no longer have a Codex branch.

- [ ] **Step 5: Run default-factory and framework contract tests**

Run:

```bash
uv run pytest tests/runtime/test_default_factory.py tests/runners/test_adapter_contract.py -q
```

Expected: pass for all three runtime types.

- [ ] **Step 6: Commit default factories**

```bash
git add ksadk/runtime/factory.py ksadk/runtime/framework_adapters.py ksadk/runners/factory.py tests/runtime/test_default_factory.py
git commit -m "feat(runtime): register supported framework adapters"
```

### Task 6：让统一 FastAPI App 以 Adapter 为唯一执行入口

**Files:**
- Modify: `ksadk/server/factory.py`
- Modify: `ksadk/server/composition.py`
- Modify: `ksadk/server/app.py`
- Modify: `tests/server/test_app_factory.py`
- Modify: `tests/server/test_app_factory_a2a.py`
- Modify: `tests/server/test_app_factory_agui.py`
- Modify: `tests/server/test_app_factory_isolation.py`
- Modify: `tests/server/test_route_manifest_parity.py`
- Modify: `tests/server/test_ui_bootstrap_agui.py`
- Modify: `tests/agui/test_runtime_adapter.py`
- Modify: `tests/test_openai_protocol_e2e.py`

**Interfaces:**
- Consumes: `RuntimeExecutor` and `RuntimeLaunchContext`.
- Produces: `RuntimeAppConfig(runtime_executor: RuntimeExecutor, launch_context: RuntimeLaunchContext)` and `app.state.runtime.executor`.

- [ ] **Step 1: Change server tests to build with an adapter/executor and no Runner**

```python
def test_runtime_app_owns_executor_not_runner(recording_executor, launch_context):
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=recording_executor,
            launch_context=launch_context,
        )
    )
    assert app.state.runtime.executor is recording_executor
    assert not hasattr(app.state.runtime, "runner")
```

- [ ] **Step 2: Run server and AG-UI tests and verify old Runner assumptions fail**

Run:

```bash
uv run pytest tests/server/test_app_factory.py tests/agui/test_runtime_adapter.py -q
```

Expected: failures mention required `runner` fields or missing executor fields.

- [ ] **Step 3: Replace Runner fields in `RuntimeAppConfig`**

The app config accepts a RuntimeExecutor and launch context. Route handlers create canonical StartRequest and call executor verbs; routes contain no Runner or framework selection branches.

- [ ] **Step 4: Move usage/session preprocessing before executor start**

Use existing `build_run_input`/`prepare_runtime_start` helpers to produce `StartRequest.metadata[CONVERSATION_PREPROCESSING_METADATA_KEY]`. Route code never calls `runner.stream()`.

- [ ] **Step 5: Run server, Responses and AG-UI tests**

Run:

```bash
uv run pytest tests/server tests/agui tests/test_openai_protocol_e2e.py -q
```

Expected: pass with no `ksadk.runners` import under `ksadk/server` or `ksadk/agui`.

- [ ] **Step 6: Commit adapter-first app composition**

```bash
git add ksadk/server/factory.py ksadk/server/composition.py ksadk/server/app.py tests/server/test_app_factory.py tests/server/test_app_factory_a2a.py tests/server/test_app_factory_agui.py tests/server/test_app_factory_isolation.py tests/server/test_route_manifest_parity.py tests/server/test_ui_bootstrap_agui.py tests/agui/test_runtime_adapter.py tests/test_openai_protocol_e2e.py
git commit -m "refactor(server): compose around runtime adapters"
```

### Task 7：将 CLI Web、run-server 和生成启动代码切到 RuntimeAdapter

**Files:**
- Modify: `ksadk/cli/cmd_web.py`
- Modify: `ksadk/cli/cmd_run.py`
- Modify: `ksadk/builders/code_builder.py`
- Modify: `ksadk/builders/container_builder.py`
- Modify: `ksadk/deployment/manager.py`
- Create: `tests/cli/test_cmd_web_runtime_adapter.py`
- Create: `tests/cli/test_cmd_run_runtime_adapter.py`
- Create: `tests/test_generated_runtime_bootstrap.py`
- Modify: `tests/test_runtime_common_packaging.py`

**Interfaces:**
- Consumes: `build_default_runtime_registry`, `RuntimeExecutor`, `RuntimeLaunchContext`, `create_runtime_app`.
- Produces: Web/server startup with no `BaseRunner.run_server()` call.

- [ ] **Step 1: Write CLI assertions against adapter factory calls**

Patch `ksadk.runtime.factory.build_default_runtime_registry` in CLI tests and assert the resulting app receives a `RuntimeExecutor` plus the detected `RuntimeLaunchContext`. Assert `create_runner` and `runner.run_server` are never called.

- [ ] **Step 2: Run CLI tests and verify current runner startup fails them**

Run:

```bash
uv run pytest tests/cli/test_cmd_web_runtime_adapter.py tests/cli/test_cmd_run_runtime_adapter.py -q
```

Expected: failures show `create_runner`/`run_server` usage.

- [ ] **Step 3: Build RuntimeLaunchContext after FrameworkDetector**

```python
context = RuntimeLaunchContext(
    runtime_type=result.type.value,
    project_dir=agent_path,
    detection=result,
    config=dict(result.raw_config or {}),
)
executor = RuntimeExecutor(build_default_runtime_registry())
app = create_runtime_app(
    RuntimeAppConfig(runtime_executor=executor, launch_context=context)
)
```

Start Uvicorn in the CLI, not through BaseRunner.

- [ ] **Step 4: Update generated container/bootstrap source strings**

Generated code imports `RuntimeLaunchContext`, `RuntimeExecutor`, `build_default_runtime_registry` and `create_runtime_app`; it must not contain `create_runner(`.

- [ ] **Step 5: Run CLI, builder and packaging tests**

Run:

```bash
uv run pytest tests/cli/test_cmd_web_runtime_adapter.py tests/cli/test_cmd_run_runtime_adapter.py tests/test_generated_runtime_bootstrap.py tests/test_runtime_common_packaging.py -q
```

Expected: pass; generated source contains RuntimeAdapter construction.

- [ ] **Step 6: Commit unified Web bootstrap**

```bash
git add ksadk/cli/cmd_web.py ksadk/cli/cmd_run.py ksadk/builders/code_builder.py ksadk/builders/container_builder.py ksadk/deployment/manager.py tests/cli/test_cmd_web_runtime_adapter.py tests/cli/test_cmd_run_runtime_adapter.py tests/test_generated_runtime_bootstrap.py tests/test_runtime_common_packaging.py
git commit -m "refactor(web): launch through runtime adapters"
```

### Task 8：将 A2A、AG-UI 和 Harness 切到核心 Executor

**Files:**
- Modify: `ksadk/cli/cmd_a2a.py`
- Modify: `ksadk/a2a/routes.py`
- Modify: `ksadk/a2a/server.py`
- Modify: `ksadk/agui/routes.py`
- Modify: `ksadk/harness/app.py`
- Modify: `ksadk/harness/config.py`
- Modify: `tests/a2a/test_a2a_protocol_e2e.py`
- Modify: `tests/harness/test_harness_app.py`
- Modify: `tests/harness/test_harness_codex.py`

**Interfaces:**
- Consumes: canonical registry/executor and RuntimeLaunchContext.
- Produces: all protocol tasks share RunHandle and RuntimeEvent semantics.

- [ ] **Step 1: Replace protocol test fixtures with RecordingRuntimeAdapter**

The fake implements the frozen six verbs and emits RuntimeEvent directly. Tests assert A2A cancel calls `executor.cancel(handle)` and AG-UI streams the same event IDs.

- [ ] **Step 2: Run A2A/Harness tests and verify runner-selection failures**

Run:

```bash
uv run pytest tests/a2a/test_a2a_protocol_e2e.py tests/harness/test_harness_app.py tests/harness/test_harness_codex.py -q
```

Expected: failures identify runner-owned app state and CodexRunner assumptions.

- [ ] **Step 3: Remove `_select_runtime_adapter` and Harness `build_runner` from public flow**

Harness exposes `build_runtime_adapter()` and `build_app()` passes it to RuntimeAppConfig. A2A CLI creates the same launch context as Web. Codex Harness asserts `CodexRuntimeAdapter`, not CodexRunner.

- [ ] **Step 4: Run all A2A, AG-UI and Harness tests**

Run:

```bash
uv run pytest tests/a2a tests/agui tests/harness -q
```

Expected: pass; architecture test reports no Runner imports in A2A/AG-UI.

- [ ] **Step 5: Commit protocol migration**

```bash
git add ksadk/cli/cmd_a2a.py ksadk/a2a/routes.py ksadk/a2a/server.py ksadk/agui/routes.py ksadk/harness/app.py ksadk/harness/config.py tests/a2a/test_a2a_protocol_e2e.py tests/harness/test_harness_app.py tests/harness/test_harness_codex.py
git commit -m "refactor(protocols): consume runtime executor"
```

### Task 9：迁移 Studio 执行并删除 Studio Runtime 层

**Files:**
- Modify: `ksadk/studio/service.py`
- Modify: `ksadk/studio/api.py`
- Modify: `ksadk/studio/shared_web.py`
- Modify: `tests/studio/test_codex_api.py`
- Modify: `tests/studio/test_api.py`
- Delete: `ksadk/studio/codex_runtime.py`
- Delete: `ksadk/studio/runtime_orchestrator.py`
- Delete: `tests/studio/test_codex_studio_runtime.py`
- Delete: `tests/studio/test_runtime_orchestrator.py`
- Create: `tests/studio/test_runtime_execution.py`

**Interfaces:**
- Consumes: core RuntimeExecutor and RuntimeLaunchContext.
- Produces: Studio-specific Build validation plus core execution, without a Studio runtime abstraction.

- [ ] **Step 1: Write Studio execution tests around core adapter factories**

```python
async def test_studio_validates_build_then_uses_core_executor(studio, recording_executor):
    studio.runtime_executor = recording_executor
    run = await studio.submit_run(build_id, "hello", session_id="s1")
    assert run.runtime_type == "codex"
    assert recording_executor.requests[0].input == "hello"
```

Cover stale Build, model not bound, stream events, cancellation, exact usage and runtime duration.

- [ ] **Step 2: Run focused Studio tests and verify CodexStudioRuntime assumptions fail**

Run:

```bash
uv run pytest tests/studio/test_runtime_execution.py tests/studio/test_codex_api.py -q
```

Expected: failures for missing core executor injection and current Runner factory fixtures.

- [ ] **Step 3: Move Build/Manifest preparation into StudioService**

StudioService loads the immutable artifact, validates manifest SHA/model allowlist, constructs RuntimeLaunchContext and StartRequest, then delegates lifecycle to RuntimeExecutor. EventStore persistence subscribes to RuntimeEvent; it is not embedded in RuntimeExecutor.

- [ ] **Step 4: Replace Runner fakes with RuntimeAdapter fakes**

Test fakes emit canonical RuntimeEvent and expose exact usage/duration. Remove `codex_runner_factory` constructor parameters from StudioService.

- [ ] **Step 5: Delete Studio runtime files and obsolete tests**

Delete only after all callers use core execution. Do not leave re-export aliases.

- [ ] **Step 6: Run Studio API, Responses and OTLP tests**

Run:

```bash
uv run pytest tests/studio/test_runtime_execution.py tests/studio/test_codex_api.py tests/studio/test_api.py tests/studio/test_otel_trace.py tests/studio/e2e -q
```

Expected: pass with canonical RuntimeEvent persistence and no Studio runtime classes.

- [ ] **Step 7: Commit Studio migration**

```bash
git add ksadk/studio/service.py ksadk/studio/api.py ksadk/studio/shared_web.py tests/studio/test_codex_api.py tests/studio/test_api.py tests/studio/test_runtime_execution.py
git add -u ksadk/studio/codex_runtime.py ksadk/studio/runtime_orchestrator.py tests/studio/test_codex_studio_runtime.py tests/studio/test_runtime_orchestrator.py
git commit -m "refactor(studio): use core runtime execution"
```

### Task 10：瘦身 BaseRunner 并删除 CodexRunner

**Files:**
- Modify: `ksadk/runners/base_runner.py`
- Modify: `ksadk/runners/adk_runner.py`
- Modify: `ksadk/runners/langgraph_runner.py`
- Create: `ksadk/runners/support.py`
- Modify: `ksadk/runtime/runner_adapter.py`
- Modify: `ksadk/runtime/preprocessing.py`
- Delete: `ksadk/runners/codex_runner.py`
- Delete: `tests/runners/test_codex_runner.py`
- Create: `tests/runners/test_base_runner_contract.py`
- Modify: `tests/runners/test_adapter_contract.py`
- Modify: `tests/runners/test_runtime_resume_cancel_e2e.py`
- Modify: `tests/test_runner.py`

**Interfaces:**
- Consumes: RunnerRuntimeAdapter internal bridge.
- Produces: BaseRunner limited to framework loading, native streaming, capabilities and close.

- [ ] **Step 1: Add a BaseRunner surface-area test**

```python
def test_base_runner_does_not_own_web_or_platform_lifecycle() -> None:
    assert not hasattr(BaseRunner, "run_server")
    assert not hasattr(BaseRunner, "invoke")
    assert not hasattr(BaseRunner, "sync_process_model_env")
    assert not hasattr(BaseRunner, "get_session_adapter")
```

- [ ] **Step 2: Run the surface test and verify current methods fail it**

Run:

```bash
uv run pytest tests/runners/test_base_runner_contract.py -q
```

Expected: failures for current platform/Web methods.

- [ ] **Step 3: Move shared helpers to owning modules**

Move the existing BaseRunner method bodies without semantic changes into module-level functions in `ksadk/runners/support.py`: `normalize_requested_model`, `sync_process_model_env`, `build_optional_call_kwargs`, `build_native_context`, `extract_usage` and `extract_last_usage`. ADK and LangGraph then import them explicitly:

```python
from ksadk.runners.support import (
    build_native_context,
    build_optional_call_kwargs,
    extract_last_usage,
    extract_usage,
    sync_process_model_env,
)
```

ADK/LangGraph import these functions explicitly. `prepare_runtime_start` uses `TranscriptReplayAdapter` when the concrete runner has no `get_session_adapter` hook. Server startup remains exclusively in CLI/server composition.

- [ ] **Step 4: Delete CodexRunner and update package exports**

Remove file, factory branch, docs strings and tests. `rg "CodexRunner" ksadk tests` must return no results.

- [ ] **Step 5: Run all runner/runtime tests**

Run:

```bash
uv run pytest tests/runners tests/runtime -q
```

Expected: pass for ADK/LangGraph/legacy adapters and direct Codex adapter.

- [ ] **Step 6: Commit runner cleanup**

```bash
git add ksadk/runners/base_runner.py ksadk/runners/adk_runner.py ksadk/runners/langgraph_runner.py ksadk/runners/support.py ksadk/runtime/runner_adapter.py ksadk/runtime/preprocessing.py tests/runners/test_base_runner_contract.py tests/runners/test_adapter_contract.py tests/runners/test_runtime_resume_cancel_e2e.py tests/test_runner.py
git add -u ksadk/runners/codex_runner.py tests/runners/test_codex_runner.py
git commit -m "refactor(runners): keep framework bridge internal"
```

### Task 11：架构、协议与全量回归验证

**Files:**
- Update: `docs/superpowers/plans/2026-08-04-runtime-adapter-web-unification.md` checkboxes.

No production-file edits belong to Task 11. If a verification command fails, reopen the task that owns the failing contract, add a failing regression test there, fix it there, and rerun Task 11 from Step 1.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: proof that Wave 0/1 of the design is complete.

- [ ] **Step 1: Verify deleted symbols and forbidden imports**

Run:

```bash
uv run pytest tests/architecture/test_runtime_boundaries.py -q
rg -n "CodexRunner|CodexStudioRuntime|StudioCodexRuntimeAdapter|StudioRuntimeAdapterRegistry|StudioRuntimeOrchestrator" ksadk tests
rg -n "ksadk\.runners" ksadk/server ksadk/studio ksadk/agui ksadk/a2a
```

Expected: test passes and both `rg` commands produce no matches.

- [ ] **Step 2: Verify RuntimeAdapter contract matrix**

Run:

```bash
uv run pytest tests/runtime tests/runners/test_adapter_interface.py tests/runners/test_adapter_contract.py tests/runners/test_runtime_resume_cancel_e2e.py -q
```

Expected: Codex, ADK and LangGraph adapter contracts pass.

- [ ] **Step 3: Verify protocol surfaces**

Run:

```bash
uv run pytest tests/server tests/agui tests/a2a tests/harness tests/test_openai_protocol_e2e.py -q
```

Expected: Responses, AG-UI, A2A and Harness pass against RuntimeAdapter.

- [ ] **Step 4: Verify Studio execution and OTLP projection**

Run:

```bash
uv run pytest tests/studio/test_runtime_execution.py tests/studio/test_codex_api.py tests/studio/test_otel_trace.py tests/studio/e2e -q
```

Expected: Studio run/session/trace tests pass with exact Runtime usage/timing.

- [ ] **Step 5: Run repository quality gates**

Run:

```bash
uv run ruff check ksadk tests
uv run mypy ksadk
node --check ksadk/studio/static/app.js
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Run the full test suite**

Run:

```bash
uv run pytest -q
```

Expected: zero failures; environment-gated network tests may skip only when their documented credentials are absent.

- [ ] **Step 7: Commit only verification-driven fixes and checked plan state**

```bash
git add docs/superpowers/plans/2026-08-04-runtime-adapter-web-unification.md
git commit -m "test(runtime): verify unified execution stack"
```

---

## Task 3 后的并行分工

Once Task 3 freezes `RuntimeLaunchContext` and the factory-backed Registry:

- Owner A: Tasks 2 and 5, Codex direct adapter/default factories.
- Owner B: Tasks 4 and 6, RuntimeExecutor/server composition.
- Owner C: Tasks 7 and 8, CLI/generated bootstrap/protocol migration.
- Owner D: Task 9, Studio migration.
- Owner E: Task 10, BaseRunner cleanup after callers migrate.

Task 11 is a single integration gate after all owners merge. No owner may independently change the frozen six RuntimeAdapter verbs.

## 后续实施计划

After this plan passes Task 11, create and execute these separate plans against the stable runtime contracts:

1. Agent Draft, Runtime Catalog and four creation flows.
2. Global Workspace/Agent/ExecutionTarget context and all-tab adaptation.
3. Tool compatibility catalog and Skill discovery/inspect/import/bind.
4. Session persistence, browser refresh recovery and multi-model interaction.
5. Trace Explorer final polish, Raw OTLP copy and full in-app-browser acceptance matrix.
