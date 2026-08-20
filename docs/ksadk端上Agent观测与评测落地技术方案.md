# KsADK 端上 Agent 观测与评测落地技术方案

> 状态：实施设计；阶段一公共评测契约、A2A 主链路和本地参考答案自动评分已落地，LocalTarget/Studio 全闭环仍按下文 Roadmap 推进。
>
> 范围：`ksadk-python` 的本地评测、CLI、本地 Studio 和运行关联。云端只定义客户端需要的接口，不在本仓实现资产管理或调度服务。
>
> 相关背景见：[技术评审方案](ksadk端上Agent观测与评测技术评审方案.md)。

## 1. 要解决的问题

开发者需要用同一份评测集检查本地 Agent，并得到可复查的结果。结果既能在 CLI 中查看，也能在 Studio 中查看；失败时可以看到这次运行的必要证据。

P1 先做三个目标切片：

1. 通用本地 Agent：复用已有 Runner、RuntimeEvent 和 Trace 查询。
2. A2A Agent：只做本地串行评测和协议错误归一，不包含云端批量调度。
3. Codex CLI：作为独立专项运行，不把 Codex 事件伪装成 RuntimeEvent。

默认只写本地文件，不自动上传报告，也不新增 Trace 外导。

| P1 要做 | P1 不做 |
| --- | --- |
| EvalSet 读取、执行、评分、报告、CLI、Studio 详情 | 云端 EvalSet CRUD、云端调度、生产流量回放 |
| 本地 Agent 评测；本地串行 A2A 评测；基于参考答案的本地自动评分 | A2A 云端批量调度、ManagedRuntime 云端 Worker、默认启用的外部 LLM Judge、趋势比较 |
| Codex 的 worktree、JSONL、diff、验证日志和安全门禁 | 修改 Codex 本体、读取 Codex 历史目录或 TUI |

## 2. 主流程

```text
EvalSet + TargetSnapshot + EvalRunSpec
                 |
                 v
          EvaluationExecutor
          /                 \
   LocalTarget      A2ATarget      CodexTarget
          \                 /
                 v
      TargetRun + MetricResult[]
                 |
                 v
           EvalRunReport
          /             \
       CLI 输出       Studio API/UI
```

流程很简单：

1. CLI 或 Studio 读取评测集和 target，生成 `EvalRunSpec`。
2. 执行器逐个运行 Case，得到 `TargetRun`。
3. 评估器根据输出、耗时和工具轨迹生成 `MetricResult`。
4. 写入 `EvalRunReport` 和 Case artifact。
5. CLI 展示文本或 JSON；Studio 只读取报告和受限 artifact。

本地 Agent 有 RuntimeEvent 或 Trace 时，报告保存 `TraceRef`。没有轨迹时返回 `UNAVAILABLE`。Codex 使用自己的 JSONL、diff 和验证日志，不转换成 RuntimeEvent。

## 3. 公共数据与文件

CLI、Studio 和 target 都使用同一组对象，不能各自定义一套结果格式。

| 对象 | 作用 |
| --- | --- |
| `EvalSetVersion` / `EvalCase` | 评测集和 Case 内容，加载后计算 `content_digest` |
| `TargetSnapshot` | 被测目标的类型、入口和版本摘要 |
| `EvalRunSpec` | 一次不可变的执行计划：评测集、目标、超时、评估器 |
| `TargetRun` | 单个 target 的输出、耗时、错误和 `TraceRef` |
| `MetricResult` | 单项评分：`PASS`、`FAIL`、`UNAVAILABLE`、`ERROR` |
| `EvalRunReport` | 唯一结果文件，CLI、Studio 和未来上传都复用它 |

运行状态使用 `PENDING -> RUNNING -> PASSED/FAILED/ERROR/CANCELLED`。

- `FAILED`：Agent 的结果不满足断言。
- `ERROR`：加载失败、超时、运行异常或协议错误。
- `UNAVAILABLE`：需要的轨迹或证据不存在。

报告和 artifact 写到项目本地目录，例如：

```text
.agentkit/evaluations/<eval_run_id>/
  report.json
  artifacts/<case_id>/<attempt>/
```

写入报告和 manifest 时使用原子写入。报告只保存摘要、相对路径和 ID，不保存凭据、环境变量、完整异常栈或原始敏感内容。

### EvalSet 示例

```yaml
schemaVersion: ksadk.eval/v1
name: weather-smoke
cases:
  - id: weather-beijing
    input: "北京今天的天气如何？"
    assertions:
      - type: response.contains
        value: "北京"
      - type: runtime.maxLatencyMs
        value: 5000
      - type: tool.called
        value: get_weather
```

支持响应、预算和工具轨迹断言。旧 Studio Suite 和 ADK EvalSet JSON 可以导入；不支持的字段必须报错，不能静默丢弃。

`expectedOutput` 是自动评分的参考答案，不是断言。`reference_output` / `referenceOutput` 是面向常见评测集的同义输入，加载后统一归一化为 `expectedOutput`。可以与 `input` 使用同一层级的紧凑格式：

```yaml
cases:
  - id: echo
    input: ping
    reference_output: pong
```

当调用方未显式传入 `--evaluator` 时，KsADK 根据每个 Case 的数据自动生成评测计划：有参考答案时使用已配置的 LLM Judge，否则使用本地 `reference_match@v1`；有 `expectedTools` 时增加 `tool_trajectory@v1`；响应、工具或运行预算断言仍作为高级的显式门禁。只有 `input`、既无参考答案也无响应断言的 Case 会生成必需的 `response_quality=UNAVAILABLE`，不能因 Agent 成功返回而显示为业务评测通过。

## 4. 模块与边界

| 模块 | 优先级 | 主要实现 | 边界 |
| --- | --- | --- | --- |
| 本地 Agent 评测 | P0-P1 / 高 | `ksadk.evaluation`：加载 EvalSet，`EvaluationExecutor` 按 Case 调用 `LocalTarget` 或 `A2ATarget`，处理超时/取消/隔离，执行断言后原子写 `EvalRunReport` 和 artifact | 不负责云端管理、HTTP 接口或 Studio 页面；只返回统一 `TargetRun` 和报告 |
| 评估器 | P1 / 高 | `ksadk.evaluation.evaluators`：对响应、JSON、延迟、Token 预算、工具轨迹和参考答案做评分，生成逐项 `MetricResult` 和证据摘要 | 不重新运行 Agent，不修改原始 `TargetRun`；缺少轨迹只能返回 `UNAVAILABLE`，不降级为通过 |
| CLI 执行 | P1 / 高 | `ksadk.cli.cmd_eval` 实现 `agentengine eval`：校验 `--agent-dir`、`--a2a-url`、`--codex-worktree` 互斥，加载 EvalSet，调用执行器，输出进度、文本报告或 JSON，并用退出码表示通过、质量失败、执行错误和证据缺失 | 不实现评分、Trace 查询或页面；CLI 与 Studio 必须读取同一份报告 |
| Studio 页面 | P1 / 高 | `ksadk.studio`：`api.py`/`api_contracts.py` 创建和查询评测，`service.py`/`operations.py` 异步执行；`static/` 提供评测列表、新建评测、运行详情、Case 详情、取消和刷新页面；`evaluation.py` 复用 build 评测 | 不复制执行器、评分逻辑或报告存储；HTTP 请求不等待 Agent 完成，页面只读报告和脱敏 artifact |
| `runtime_observability` | P1-P3 / 中低 | 通用 Agent 的上下文、TraceRef、查询和外导 | 不创建第二份 Trace 存储 |
| 控制面客户端 | P2 / 中 | 拉取评测集、结果补传、上传回执和补传队列 | 不访问开发机源码、凭据或 worktree；A2A 本地调用由 `A2ATarget` 负责 |

建议的代码位置：

```text
ksadk/
├── evaluation/
│   ├── contracts.py
│   ├── evalset.py
│   ├── executor.py
│   ├── targets.py
│   ├── evaluators.py
│   ├── storage.py
│   ├── context.py
│   ├── codex.py
│   ├── codex_events.py
│   └── verification.py
└── cli/
    └── cmd_eval.py
```

现有 `studio/evaluation.py`、Studio Operation 和 Event/Trace 查询继续复用。不要为 P1 新建数据库、后台队列、插件系统或新的微服务。

### 4.1 本地 Agent 评测怎么实现（P0-P1）

本地评测由 `EvaluationExecutor` 统一编排，目标是让 CLI、Studio 和后续端云上传都使用同一份结果。

1. `evalset.py` 读取 EvalSet，校验 schema，并计算 `content_digest`；非法字段直接返回错误。
2. `executor.py` 为每个 Case 创建独立的 `EvaluationContext` 和 session，设置超时，调用 `LocalTarget`；Case 结束后释放 session 和临时资源。
3. `targets.py` 将本地 Agent 入口适配成统一调用协议，记录输入、输出、状态、耗时、usage 和 `TraceRef`，失败转换为明确错误码。
4. `evaluators.py` 按断言类型逐项评分，单项结果写入 `MetricResult`；没有所需轨迹时标记 `UNAVAILABLE`，不猜测为通过。
5. `storage.py` 先写 Case artifact 和 manifest，再原子写 `report.json`；执行中断时保留已完成 Case，并把运行标为 `ERROR` 或 `CANCELLED`。

P1 先支持单机串行执行；并发、重试和批量调度不放入首版。建议文件为 `ksadk/evaluation/contracts.py`、`evalset.py`、`executor.py`、`targets.py`、`evaluators.py` 和 `storage.py`。

### 4.2 评估器怎么实现（P1）

评估器只消费 `TargetRun` 和可用的 RuntimeEvent/Trace，不参与 Agent 调用。未指定 `--evaluator` 时，评测器由 EvalSet 数据自动推导；显式选择的评估器始终覆盖自动计划。

1. 有 `expectedOutput`（包括 `reference_output` 别名）时自动评估响应质量：Judge 配置完整时使用 `llm_judge@v1`，否则回退为本地 Rouge-1 F1 `reference_match@v1`，阈值为 `0.8`。
2. 有 `expectedTools` 或工具断言时自动执行 `tool_trajectory@v1`；没有可查询的 Trace 时返回 `UNAVAILABLE`，不根据最终文本猜测工具调用。
3. `runtime_budget@v1` 保留为显式 SLA 门禁，仅在最大延迟或 Token 断言存在时执行；所有运行的实际耗时和 usage 仍记录在 `TargetRun`，但没有预算时不虚构 PASS/FAIL。
4. 响应包含/相等、JSON Schema、工具禁止调用等 assertions 保留为高级约束。它们可以与自动质量评测并行，但不是普通参考答案评测的必填输入。
5. 既无参考答案也无响应断言的 Case 返回必需 `response_quality=UNAVAILABLE`。Case 不会因 Agent 成功执行而被报告为业务质量 `PASSED`。
6. 每个指标返回 `MetricResult`，至少包含状态、得分和简短 evidence；Case 只有 Target 成功且所有必需指标通过才算通过。
7. `llm_judge@v1` 需要执行 `uv sync --extra judge`、显式 `dataPolicy=full_trace`、Judge 模型、API Base 和密钥环境变量。当前实现以 DeepEval 为执行引擎；条件不完整时自动模式选择本地参考答案匹配，显式指定 Judge 时才返回 `UNAVAILABLE`。Judge 不覆盖确定性断言、工具轨迹或运行预算门禁。

评估器的输入是 EvalSet 中的 assertion、参考答案和 `TargetRun`，输出写入 `EvalRunReport.results`；不保存完整 Prompt、Reasoning、Judge 理由或敏感工具返回。测试重点覆盖边界值、类型错误、缺失证据和多断言聚合。

### 4.3 CLI 执行怎么实现（P1）

CLI 只负责把命令行参数转换成 `EvalRunSpec`，不在 CLI 内复制执行器或评分逻辑。

1. 解析 `--evalset-file`、`--agent-dir`、`--a2a-url`、`--report-dir`、`--format` 和超时参数，并校验 target 参数互斥。
2. 调用 `EvaluationExecutor.run()`；默认输出简短进度，结束后从 `report.json` 渲染结果。
3. 不传 `--evaluator` 时，CLI 在 `--validate-only` 输出中给出由 EvalSet 推导的 `evaluationPlan`。只提供 `input + reference_output` 会自动使用本地参考答案匹配；配置 `--data-policy full_trace --judge-model <model> --judge-api-base <url>` 和密钥环境变量后自动改用语义 Judge。也可显式设置 `--evaluator` 覆盖自动计划；命令行和报告均不接收密钥值。
4. `--format pretty|text` 输出通过率、失败 Case 和错误摘要；`--format json` 输出完整 `EvalRunReport`，供 CI 或脚本读取。`text` 是 `pretty` 的兼容别名。
5. 根据报告状态返回固定退出码：全部通过为 `0`，质量失败为 `1`，配置/执行错误为 `2`，必需证据缺失为 `3`。
6. CLI 只输出脱敏摘要；详细 artifact 通过 `--report-dir` 指定的报告目录保存，不把 token、环境变量或完整异常栈写到终端。

实现入口为 `ksadk/cli/cmd_eval.py`，报告读写复用 `ksadk.evaluation.storage`。CLI 和 Studio 使用同一份报告做一致性验收。

### 4.4 Studio 页面怎么实现（P1）

Studio P1 是本地评测的操作界面，执行由本地 Operation 异步承载，页面不直接运行 Agent。

1. 新建评测页面提交 EvalSet 和 target 引用；build Agent 复用 `POST /api/v1/builds/{build_id}/evaluations`，独立 A2A 使用 `POST /api/v1/evaluations`。
2. API 创建 Operation 后立即返回 `operation_id`/`run_id`，前端通过 Operation 查询或 SSE 刷新状态，不在 HTTP 请求中等待执行完成。
3. 评测列表读取本地报告索引，展示 EvalSet、target、创建时间、状态和通过率；运行详情展示汇总、错误摘要和 Trace 可用性。
4. Case 详情只读取报告中的脱敏字段和 artifact 白名单，展示输出、断言、耗时和相对路径；缺失证据显示 `UNAVAILABLE`。
5. 取消操作调用现有 `POST /api/v1/operations/{operation_id}:cancel`；页面刷新或 Studio 重启后根据 `run_id` 重新读取报告。

实现主要复用 `ksadk/studio/api.py`、`api_contracts.py`、`service.py`、`operations.py`、`evaluation.py` 和 `static/app.js`；不新增数据库或第二套评测结果模型。

### 4.5 观测导出怎么实现（P3）

观测导出不放在 Agent 主流程里同步发送，避免 Collector 故障拖慢评测。实现分四步：

1. 运行入口创建或结束 Span 时，先写本地 Trace/Span 和 `TraceRef`。
2. `redaction.py` 按字段白名单脱敏，只允许 run/case/target/status/duration 等摘要字段；删除 token、环境变量、Prompt、Reasoning 和文件正文。
3. `trace_exporter.py` 把脱敏后的 Span 放入有上限的内存队列，批量通过 OTLP/HTTP 发送；队列满时丢弃导出任务，不影响本地报告。
4. `health.py` 记录队列长度、最近成功时间、失败次数和丢弃次数，供 Studio 或健康接口读取。

导出使用 `trace_id + span_id` 去重。发送超时和重试次数有上限，进程退出时只做短暂 flush，不保证导出成功。导出开关默认关闭，报告上传和 Trace 导出是两套配置。

建议文件：`runtime_observability.py` 负责关联，`redaction.py` 负责脱敏，`trace_exporter.py` 负责发送，`health.py` 负责状态。

### 4.6 端云结合怎么实现（P2）

端云只同步评测资产和结果，不把本地 Agent、worktree 或凭据交给云端。

```text
控制面拉取固定版本 EvalSet
        -> 本地 CLI/Studio 执行
        -> 本地保存 EvalRunReport
        -> 可选上传 result_only 报告
        -> 云端返回回执；失败进入本地补传队列
```

建议客户端接口：

| 接口 | 用途 | 关键规则 |
| --- | --- | --- |
| `GET /api/v1/evalsets/{id}/versions/{version}` | 拉取不可变 EvalSet | 响应带 `content_digest`；本地校验后才执行 |
| `PUT /api/v1/evaluation-runs/{run_id}/report` | 上传 `result_only` 报告 | 只传允许字段和摘要，不传 worktree、凭据和原始 Trace |
| `GET /api/v1/evaluation-runs/{run_id}/receipt` | 查询上传回执 | 回执包含 accepted、duplicate、rejected 和字段错误 |

上传使用 `Idempotency-Key = eval_run_id + attempt + report_digest`。网络失败写入 `.agentkit/evaluations/outbox/`，按指数退避重试；上传失败不重跑 Agent。凭据只使用 credential reference 或环境注入，不写入报告和快照。

### 4.7 A2A Agent 本地评测（P1）

A2A 本地评测只需要一个 Agent Card 地址和测试输入，不依赖云端控制面：

1. 读取 Agent Card，记录能力和协议版本摘要。
2. 根据 Case 输入发送 Message 或 Task；支持同步响应、流式响应和轮询任务。
3. 处理超时、取消、服务端错误和连接断开。
4. 将响应、耗时、状态和可用的 usage/Trace 归一化成 `TargetRun`。
5. 运行通用响应、预算和工具轨迹评估器；远端没有工具轨迹时返回 `UNAVAILABLE`。

`--agent-dir`、`--a2a-url` 和 `--codex-worktree` 三者互斥。A2A 客户端只访问用户明确提供的本地或受信地址，不把远端 Agent 当作云端托管能力。

## 5. Codex CLI 独立专项

Codex 不放进通用任务表。它单独排期、单独做安全评审，只复用公共的 `EvalRunSpec`、`TargetRun` 和 `EvalRunReport`。

每个 Case 的基本流程：

```text
检查配置 -> 建立 detached worktree -> codex exec --json
       -> 保存脱敏证据 -> 检查 diff/验证命令 -> 写报告 -> 清理 worktree
```

执行时使用参数数组调用子进程，不使用 `shell=True`、`--yolo` 或任意 shell 文本。`--codex-worktree` 必须是 Git 仓库；`--codex-profile` 和验证命令只能引用受信配置。

```text
codex exec --json --sandbox workspace-write -C <case-worktree>
  --output-last-message <artifact-dir>/final.md <case-prompt>
```

| ID | 阶段/优先级 | 要做什么 | 交付 |
| --- | --- | --- | --- |
| CDEX-001 | P0 / 最高 | 定义 Codex Case、允许路径、禁止路径和验证 profile | 非 Codex target 不能使用这些字段；EvalSet 不能传任意 shell |
| CDEX-002 | P0 / 最高 | 检查 Codex、Git、profile、sandbox；为每个 Case 建独立 worktree | 基线目录不被改动；失败在执行前返回明确错误 |
| CDEX-003 | P0 / 最高 | 解析 JSONL 并脱敏 | 保存事件类型、状态和 ID；去掉 reasoning、凭据、Prompt 和工具返回 |
| CDEX-004 | P0 / 高 | 保存证据包并建立 `codex.run` Span | 保存 `events.jsonl`、`final.md`、`stderr.log`、`diff.patch`、验证日志和 manifest |
| CDEX-005 | P1 / 最高 | 检查进程状态、改动路径、未跟踪文件、密钥扫描和验证命令 | 任一安全或执行门禁失败都阻断结果 |
| CDEX-006 | P1 / 高 | 提供 CLI 和 Studio 入口 | CLI/Studio 只能使用受信 worktree/profile，且都读取同一份报告 |
| CDEX-007 | P1 / 高 | fake CLI 和真实 CLI 验收 | fake 覆盖异常分支；真实 E2E 必须在认证和模型可用时单独记录 |

Codex artifact 只在本地保留。Studio 通过受限 API 读取脱敏内容；证据已清理或无权限时显示 `UNAVAILABLE`。

## 6. 接口约定

### CLI 和 Studio

| 入口 | P1 方式 |
| --- | --- |
| 本地 Agent CLI | `agentengine eval --agent-dir <dir> --evalset-file <file> --report-dir <directory> --format pretty|json` |
| A2A Agent CLI | `agentengine eval --a2a-url <url> --evalset-file <file> --report-dir <directory> --format pretty|json`；与其他 target 参数互斥 |
| Codex CLI | 见 CDEX-006，使用 `--codex-worktree` 和受信 `--codex-profile` |
| Studio | build 评测复用 `POST /api/v1/builds/{build_id}/evaluations`；A2A 评测使用 `POST /api/v1/evaluations`；详情通过 `GET /api/v1/evaluations/{id}` 和 Case API 读取 |
| 通用观测查询 | P2 提供 `agentengine observe` 和 `GET /api/v1/observations/{runId}` |

CLI 退出码：`0` 全部通过，`1` 质量失败，`2` 执行或配置错误，`3` 必需证据不可用。

评测报告位置只使用 `--report-dir`；`--format` 只控制终端渲染。根 CLI 的全局 `--output` 保持既有终端输出格式语义，不承担报告路径语义。

观测导出使用 OTLP/HTTP，配置至少包含 `enabled`、`endpoint`、`timeout_ms`、`queue_size` 和 `redaction_policy`。导出接口只接收脱敏 Span；健康接口返回 `enabled`、`queue_depth`、`last_success_at`、`failed_count` 和 `dropped_count`。

端云客户端至少提供 `pull_evalset(version)`、`upload_report(report, result_only=True)`、`get_receipt(run_id)` 和 `retry_outbox()` 四个方法。服务端不提供“远程执行本地 Agent”的接口。

### 6.1 Studio P1 能力

Studio P1 只做本地评测的发起和查看，执行仍由本地 Runner 完成。页面不复制评分逻辑，也不直接读取 Agent 工作区。

**页面和流程**

1. 评测列表：展示 `run_id`、EvalSet、target、创建时间和状态，支持按状态筛选。
2. 新建评测：选择本地 EvalSet、`--agent-dir` 或 `--a2a-url`，填写超时和评估器；提交前校验 target 参数互斥。
3. 运行详情：展示总体通过率、耗时、错误摘要和 `TraceRef` 可用性。
4. Case 详情：展示输入、脱敏后的输出、断言结果、耗时、错误码和 artifact 相对路径；证据不存在显示 `UNAVAILABLE`。
5. 刷新状态：通过 Operation 轮询本地运行状态；页面刷新或重启后可根据 `run_id` 继续读取报告。

流程为：`POST 创建 -> Operation 返回 run_id -> 本地执行 -> 原子写 report.json -> GET 列表/详情`。Studio 只展示已写入的报告，不在请求线程等待 Agent 执行。

**P1 接口和模型**

| 接口/模型 | P1 约定 |
| --- | --- |
| `POST /api/v1/builds/{build_id}/evaluations` | 复用现有 build 评测入口；入参为 `EvaluationRequest{suite_refs, concurrency, fail_fast}`，要求 `Idempotency-Key`，返回 `202 Operation` |
| `POST /api/v1/evaluations` | P1 新增独立 A2A 入口；入参为 `StudioEvaluationCreate{evalset_ref, target_ref, timeout_ms}`，返回 `202 Operation` |
| `GET /api/v1/evaluations` | 返回本地报告摘要，支持 `status`、EvalSet 和 target 筛选；列表索引从本地 manifest 读取 |
| `GET /api/v1/evaluations/{run_id}` | 返回 `EvalRunReport` 摘要、阶段状态和错误摘要 |
| `GET /api/v1/evaluations/{run_id}/cases/{case_id}` | 返回单 Case 的 `TargetRun`、`MetricResult[]` 和允许展示的 artifact 索引 |
| `GET/POST /api/v1/operations/{operation_id}(:cancel)` | 复用现有 Operation 查询和取消接口；终态取消返回幂等成功 |
| `EvaluationRequest` | 现有 build 评测模型；P1 只扩展校验，不改变 `suite_refs`、`concurrency`、`fail_fast` 含义 |
| `StudioEvaluationCreate` | 仅用于独立 A2A 入口，只包含 EvalSet 引用、A2A 地址和超时；不接受任意 shell、凭据和原始 Prompt 模板 |
| `EvaluationRun` / `EvaluationCaseResult` | 兼容现有 Studio 读模型，再投影为公共 `EvalRunReport`，不新增第二套评分结果 |

实现边界：API 层负责参数校验和 Operation，build 入口调用现有 `EvaluationRunner`，独立 A2A 入口调用 `EvaluationExecutor/A2ATarget`；存储层复用 `storage.py` 的报告和 manifest；Case 详情通过 artifact 白名单读取，禁止把绝对路径或工作区文件直接暴露给浏览器。

**Studio P1 开发任务**

| ID | 优先级 | 任务 | 交付和验收 |
| --- | --- | --- | --- |
| STUDIO-101 | P1 / 高 | 对齐现有 `EvaluationRequest`，增加独立 A2A 创建请求和统一响应模型 | build 入口保持兼容；A2A 入口校验 `target_ref`、地址和超时；错误码可定位 |
| STUDIO-102 | P1 / 高 | 接入本地 Operation 和执行器 | build 入口复用 `EvaluationRunner`，A2A 入口调用 `EvaluationExecutor`；创建立即返回 `202 Operation`，重启后可查询 |
| STUDIO-103 | P1 / 高 | 实现评测列表、新建评测和运行详情 | LocalTarget/A2ATarget 都能发起；列表、详情与 CLI 读取同一 `EvalRunReport` |
| STUDIO-104 | P1 / 中 | 实现 Case 详情和 artifact 白名单 | 展示脱敏输出、断言和错误摘要；越权路径、缺失证据分别返回 403/`UNAVAILABLE` |
| STUDIO-105 | P1 / 中 | 取消、刷新和异常状态处理 | 覆盖超时、取消、执行错误、报告损坏；不把异常栈或凭据返回前端 |

## 7. 分阶段实现 Roadmap

本节使用“产品阶段”表示可独立交付的能力闭环，保留 P0-P3 表示任务顺序和优先级。两者映射如下：

| 产品阶段 | 对应任务 | 交付目标 | 网络与数据默认 |
| --- | --- | --- | --- |
| 阶段一：端上本地评测闭环 | P0 + P1 | CLI、Studio 使用同一执行器和报告契约，完成本地 Agent、本地串行 A2A、Codex 专项评测和本地参考答案自动评分 | 报告和证据默认只落本地，`local_only`，不新增 Trace 外导 |
| 阶段二：端云资产与结果协作 | P1.5 + P2 | 精确版本 EvalSet 下发、`result_only` 上传、本地/云端 Worker 契约对齐和通用运行查询 | 执行器位于可访问 Target 的一侧；云端不反向访问本地 Agent |
| 阶段三：观测外导与评测治理 | P3 | OTLP 外导、可比较实验、质量门禁、Judge、趋势和数据治理 | Trace 外导与报告上传独立授权，未显式开启时仍只保留本地数据 |

实现顺序固定为：`契约 -> 本地执行闭环 -> 端云同步 -> 云端 Worker -> 外导和治理`。后一阶段不得为了先上页面或服务而复制前一阶段的执行器、评分逻辑或报告模型。

### 7.1 阶段一：端上本地评测闭环

阶段一的实施细节以第 3-6 节为准，本节只定义实施顺序和阶段退出条件。

| 实施步骤 | 建设内容 | 对应任务 | 前置依赖 |
| --- | --- | --- | --- |
| 1A 契约和本地存储 | 冻结 EvalSet、TargetSnapshot、EvalRunSpec、TargetRun、MetricResult 和 EvalRunReport；完成旧 Suite/ADK 导入、digest、报告与 artifact 原子写入 | GEN-001、GEN-002 | 无 |
| 1B 执行与确定性评分 | 实现 EvaluationExecutor、LocalTarget、Case 隔离、超时/取消、响应/预算/工具轨迹评估器 | GEN-101、EVAL-101 | 1A |
| 1C CLI 闭环 | 实现 `agentengine eval`、target 互斥校验、text/JSON 输出和固定退出码 | GEN-103 | 1A、1B |
| 1D Studio 闭环 | 复用 Operation，提供创建、列表、运行详情、Case 详情、取消和 artifact 白名单 | STUDIO-101 至 STUDIO-105 | 1A、1B |
| 1E 本地观测关联 | 写入 EvaluationContext 和 TraceRef，使 CaseRun 可下钻 RuntimeEvent/Trace；缺少证据统一返回 `UNAVAILABLE` | OBS-101 | 1B |
| 1F A2A 本地串行评测 | 读取 Agent Card，处理 Message/Task、流式/轮询、超时/取消和协议错误，归一为 TargetRun | GEN-201 | 1A、1B |
| 1G Codex 独立专项 | 隔离 worktree，执行 `codex exec --json`，保存脱敏 JSONL、diff、验证日志和独立安全门禁 | CDEX-001 至 CDEX-007 | 1A；不阻塞 1B-1F |

阶段一首版保持单机串行。A2A 在本阶段只是一种由本地执行器调用的 Target，不包含云端批量调度、ManagedRuntime 发现或远端内部 Trace 保证。`EvaluationRequest.concurrency` 保留兼容，首版只接受串行值，不提前承诺并发语义。

**阶段退出标准**

- CLI 和 Studio 运行同一 fixture 时使用同一 EvaluationExecutor，并生成等价 EvalRunReport、Case 结果和错误分类。
- 本地 Agent 和 A2A Agent 都能生成统一报告；A2A 远端轨迹缺失时显式返回 `UNAVAILABLE`。
- 每个 Case 使用隔离 session；质量失败、执行错误、超时、取消和证据缺失不互相混合。
- 有 RuntimeEvent、Trace 或 Codex JSONL 的 Case 本地关联率为 100%；无证据时不根据最终输出补造轨迹。
- Codex fake CLI 覆盖成功、失败、超时、取消、JSONL 截断、越界改动、脱敏和验证失败；真实 E2E 只在认证、模型和 Git fixture 具备时执行并单独留记录。
- 相关单测、CLI/Studio smoke 和可执行 E2E 在项目 `uv run` 环境中验证通过；报告和 API 不包含凭据、Reasoning、完整异常栈或绝对工作区路径。

### 7.2 阶段二：端云资产与结果协作

阶段二采用“资产上云、执行靠近 Target”。先完成评测集和报告同步，再接入云端 Worker；不以云端服务可见为理由改变端上执行边界。

| 实施步骤 | `ksadk-python` 交付 | 外部系统交付 | 对应任务 |
| --- | --- | --- | --- |
| 2A 精确版本 EvalSet 下发 | `pull_evalset(version)`、digest 校验、本地缓存和错误语义 | 评测控制面提供不可变 EvalSetVersion、项目权限和版本查询 | CLOUD-201 |
| 2B `result_only` 报告同步 | 脱敏投影、幂等键、receipt、outbox 和失败补传 | 评测控制面提供幂等接收、字段校验和 accepted/duplicate/rejected 回执 | CLOUD-201 |
| 2C 通用运行查询 | 按 run/case/attempt 查询 RuntimeEvent，提供 CLI/Studio 查询和 TraceRef 跳转 | 云端只保存已上传的摘要和受权 Trace 链接 | OBS-201 |
| 2D A2A/ManagedRuntime 统一 Target | 复用 A2ATarget 并增加 ManagedRuntime TargetSnapshot、鉴权引用和远程错误归一 | `agentengine-server` 负责 runtime lifecycle、gateway resolve、鉴权和可见性 | 待按跨仓契约拆分 |
| 2E 本地/云端 Worker 对齐 | 本地 Worker 执行固定 EvalRunSpec，输出统一 EvalRunReport | 评测控制面负责云端 Worker 调度、测试租户、重试/取消和权限 | 待按跨仓契约拆分 |

`ksadk-python` 不实现云端 EvalSet CRUD、资源治理后台或 Worker 调度服务。跨仓实现前必须先冻结 EvalSet 下发、EvalRunSpec、EvalRunReport 上传、receipt、鉴权引用和失败码；没有契约验收时不得以本地 mock 宣称端云链路已打通。

**阶段退出标准**

- CLI/Studio 只能使用精确版本 EvalSet 执行，本地校验的 `content_digest` 与服务端一致。
- 本地 Worker 和云端 Worker 对同一报告 schema 完成契约验收；云端 Worker 只执行其可访问的 A2A/ManagedRuntime Target。
- 报告重复上传返回 duplicate 而不生成重复运行；网络失败进入 outbox，补传不重跑 Agent。
- 云端不反向连接本地 Target，报告不上传 worktree、凭据、原始 Trace 或未脱敏业务内容。
- A2A 网络、鉴权、协议、Task 和业务错误可区分；只有远端声明并验证 W3C Trace Context 时才传播 Trace Context。
- 使用预发评测控制面、A2A/ManagedRuntime 和真实鉴权完成端云 E2E；条件不具备时明确记录缺失的服务、地址、凭据或部署版本。

### 7.3 阶段三：观测外导与评测治理

阶段三在稳定的运行契约和端云同步之上建设观测外导、比较和治理。Judge 是增量评估器，不替换确定性、安全、可启动性和证据完整性门禁。

| 实施步骤 | `ksadk-python` 交付 | 外部系统交付 | 对应任务 |
| --- | --- | --- | --- |
| 3A 可比较运行 | 实现 Snapshot 校验和 comparison 投影；稳定性比较固定全部快照，版本对比只允许 Target 版本变化 | 控制面提供实验、基线、趋势和报告 | GEN-301 |
| 3B Judge 插件 | 冻结 Judge 输入、模型、Prompt、阈值、成本和人工复核信息，输出独立 MetricResult | 控制面负责 Judge 凭据、配额、审批和可见性 | GEN-301 |
| 3C 反馈与数据治理 | 提供 FeedbackCandidate/CaseProvenance 客户端契约和脱敏投影 | 控制面负责审核、新 EvalSetVersion、项目权限、保留和删除 | GEN-301 |
| 3D OTLP 外导 | 实现字段 allowlist、结构化脱敏、有界队列、批量发送、有限重试、flush 和健康状态 | Collector/观测后端负责 OTLP 接收、路由、存储和受权查询 | OBS-301 |
| 3E 可运维与访问治理 | 暴露 enabled、queue depth、最近成功/失败时间、失败数和丢弃数，不暴露 Token/Header 值 | 观测和控制面负责采样、保留、告警、成本和访问审计 | OBS-301 |

**阶段退出标准**

- 比较前强制校验 EvalSetVersion、TargetSnapshot、评估器配置和环境摘要；不可比的运行只能并列展示。
- Judge 结果不覆盖确定性指标；安全、工作区、可启动性、验证失败和必需证据缺失保持独立阻断。
- OTLP 默认关闭；Exporter 故障、队列满或 flush 超时不改变 Agent 和评测结果。
- 脱敏失败或出现未知字段时进入 `EXPORT_BLOCKED`，不得降级为明文外发；报告上传和 Trace 外导使用独立开关和凭据。
- Callback 和直接 OTLP 不重复发送同一 Trace；同一 Trace 进入多后端时由 Collector 分流。
- 能够按项目执行采样、保留、删除和访问审计，且审计日志不包含原始敏感内容。

### 7.4 跨阶段发布门禁

每个阶段必须同时通过以下门禁，不使用综合平均分抵消安全或可用性失败：

| 门禁 | 要求 |
| --- | --- |
| 契约兼容 | schema 和错误码有版本；新字段不得被旧客户端静默丢弃 |
| 可复现性 | 报告冻结 EvalSet、Target、评估器和环境摘要；比较时先验证快照 |
| 执行安全 | Case/session/worktree 隔离；超时、取消和资源清理有确定终态 |
| 数据安全 | 报告、artifact、API、日志和 Trace 不包含未授权的凭据、Prompt、Reasoning、文件正文或客户数据 |
| 故障隔离 | 同步、Exporter、Judge 或云端控制面故障不改变已完成的本地 Agent 结果 |
| 真实验证 | 按受影响范围执行单测、smoke 和 E2E；无法执行 E2E 时记录缺失条件，不用 mock 结果代替 |

### 7.5 预期完成时间

|**阶段**|**建议周期**|**主要时间分配**|**预期完成条件**|
|---|---|---|---|
|阶段一：端上本地评测闭环|1-2周|P0 契约与存储；Executor/评估器；CLI/Studio/A2A ；Codex fake 与集成验收|本地 Agent、本地串行 A2A、CLI、Studio 共用报告并通过阶段一退出标准；Codex fake 验收通过|
|阶段二：端云资产与结果协作|2周|跨仓契约与 API；客户端同步/outbox；ManagedRuntime/Worker；预发 E2E |EvalSet 下发、报告幂等上传、补传、A2A/ManagedRuntime 和云端 Worker 通过真实契约验收|
|阶段三：观测外导与评测治理|2周|比较与质量门禁；OTLP 脱敏/外导/健康；Judge/反馈/权限；安全与运维验收 |外导故障不影响评测；比较快照可校验；Judge、反馈、保留和访问审计通过治理验收|


按 2026-08-17 作为阶段一正式启动日的参考排期如下：

|**时间窗口**|**对应交付**|
|---|---|
|2026-08-10 至 2026-08-20|阶段一完成；若仅交付本地 Agent + CLI 和基础 Studio，可在 2026-09-21 至 2026-09-28 先交付 MVP|
|2026-08-20 至 2026-09-05|阶段二完成；前提是评测控制面、ManagedRuntime 和预发凭据按计划可用|
|2026-09-05 至 2026-09-20|阶段三完成；该日期包含 OTLP 后端、数据治理和 Judge 审核的联合验收|


**时间调整规则**：阶段一的 1A、1B 和 1C 是关键路径，不建议为了等待 A2A 或 Codex 真实 E2E 而停止本地 Agent MVP；阶段二的云端 Worker 和阶段三的 OTLP/Judge 可并行开发，但阶段退出必须等待对应外部依赖完成真实验收。

## 8. 开发任务清单（可单独加载）

以下只列通用 Agent 任务。Codex 任务见第 5 节。

| ID | 阶段/优先级 | 要做什么 | 交付 |
| --- | --- | --- | --- |
| GEN-001 | P0 / 高 | 定义公共模型、状态、错误码和 EvalSet loader | `contracts.py`、`evalset.py`；旧 Suite 可导入，非法字段有诊断 |
| GEN-002 | P0 / 高 | 写报告和基础测试 | `storage.py` 原子写；覆盖 loader、状态和报告错误分支 |
| GEN-101 | P1 / 高 | 实现通用执行器和 LocalTarget | `executor.py`/`targets.py`；Case 隔离、超时、取消、统一 `TargetRun` |
| EVAL-101 | P1 / 高 | 实现确定性评估器和 Case 聚合 | `evaluators.py`；覆盖响应、JSON、预算和工具轨迹，每项返回状态和证据，缺失轨迹为 `UNAVAILABLE` |
| GEN-103 | P1 / 高 | 接通 CLI 和公共执行器 | `cmd_eval.py`；CLI 与 Studio 共用 `EvaluationExecutor`、`EvalRunReport`，不重复评分逻辑 |
| STUDIO-101 | P1 / 高 | 对齐 `EvaluationRequest`，增加独立 A2A 创建请求和统一响应模型 | build 入口保持 `suite_refs/concurrency/fail_fast` 兼容；A2A 校验 `target_ref`；错误码固定 |
| STUDIO-102 | P1 / 高 | 接入现有 Operation 和两类执行器 | build 入口复用 `EvaluationRunner`，A2A 入口调用 `EvaluationExecutor`；创建返回 `202 Operation`，重启后可查询 |
| STUDIO-103 | P1 / 高 | 实现评测列表、新建评测和运行详情 | LocalTarget/A2ATarget 可发起；页面与 CLI 展示同一报告 |
| STUDIO-104 | P1 / 中 | 实现 Case 详情、artifact 白名单和取消 | 只展示脱敏内容；越权路径拒绝，缺失证据为 `UNAVAILABLE` |
| STUDIO-105 | P1 / 中 | 覆盖刷新、取消、超时和异常状态 | 不返回凭据、完整异常栈或绝对工作区路径 |
| OBS-101 | P1 / 中 | 只补通用 `EvaluationContext` 和 `TraceRef` 字段 | 不阻塞本地评测；缺失轨迹返回 `UNAVAILABLE` |
| GEN-201 | P1 / 高 | 实现 `A2ATarget` | `a2a.py` 读取 Agent Card；支持 Message/Task、流式/轮询、超时/取消；用 mock server 覆盖错误分支并归一化为 `TargetRun` |
| CLOUD-201 | P2 / 中 | 评测集和报告同步 | `cloud_client.py`/`outbox.py` 实现 `pull_evalset`、digest 校验、`upload_report(result_only)`、回执、幂等键和补传 |
| OBS-201 | P2 / 中 | 采集通用 RuntimeEvent，提供 CLI/Studio 查询 | `runtime_observability.py`/观察 API；按 run/case/attempt 查询，不直接读取工作区 |
| GEN-301 | P3 / 中低 | 比较、Judge、反馈治理 | `comparison.py`/治理接口；比较前校验 EvalSet/Target/环境摘要；反馈需审核 |
| OBS-301 | P3 / 低 | OTLP 外导和健康检查 | `trace_exporter.py`、`redaction.py`、`health.py` 使用有界队列、脱敏白名单、超时/重试上限；外导失败不影响 Agent 或评测 |

### 分工

Codex 专项由独立负责人完成，不占下面三人组的任务。

| 角色 | P0-P1 | P2-P3 |
| --- | --- | --- |
| 1 号：本地 Agent 评测闭环 | GEN-001、GEN-002、GEN-101、EVAL-101、GEN-201 | CLOUD-201 的报告同步客户端；Judge 输入契约 |
| 2 号：CLI/Studio 集成与质量 | GEN-103、STUDIO-101 至 STUDIO-105；通用 fixture、CLI/Studio 一致性验收 | 比较页、反馈审核和数据治理 |
| 3 号：观测 | OBS-101，仅做字段约定 | OBS-201、OBS-301；先做查询，再做 OTLP，均不阻塞 P1 |

通用三人组的顺序：先完成公共契约和 CLI/Studio 入口，再完成 LocalTarget 与 A2ATarget 的 P1 评测闭环；观测查询放到 P2，OTLP 与治理放到 P3。

## 9. 验收和安全门槛

| 项目 | 最少验证 |
| --- | --- |
| 公共评测 | 单元测试覆盖模型、EvalSet、报告和评分；使用项目 `uv run pytest` |
| 本地 Agent | CLI 和 Studio 跑同一 fixture；比较报告、Case 结果和退出码 |
| 观测 | 有 Trace 时能查询；没有 Trace 时返回 `UNAVAILABLE`，不显示伪造数据 |
| Codex fake | 覆盖成功、失败、超时、取消、JSONL 截断、越界改动、脱敏和验证失败 |
| Codex 真实 E2E | 仅在认证、模型和 Git fixture 都可用时运行；否则记录缺失条件 |
| 数据安全 | 不写 token、密钥、环境变量、完整 Prompt、Reasoning 或原始敏感日志；默认 `local_only` |

以下情况必须阻断：

- Codex 在基线 worktree 中运行，或验证命令来自 EvalSet 的任意 shell 文本。
- Case 之间共享 session、状态或工具事件。
- 报告或 API 输出包含凭据、原始敏感内容或异常栈。
- 轨迹、JSONL 或验证结果缺失却被记为通过。

## 10. 参考

- [Codex 非交互 JSONL 输出](https://learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable)
- [Codex `exec` 参数](https://learn.chatgpt.com/docs/developer-commands#codex-exec)

## 11. 2026-08-20 实施状态与验收记录

本轮没有修改 Studio UI。公共执行器仍是唯一执行入口，Local Source、Studio Build 和 A2A 都输出同一 `TargetRun -> MetricResult -> EvalRunReport` 合同。

### 自动标准与 V2 门槛

当 CLI 未指定 `--evaluator` 时，执行器从 EvalSet 行自动推导所需评估器：参考答案优先使用已配置的 `llm_judge@v1`，否则使用确定性的 `reference_match@v1`；响应、运行时和工具标准分别启用 `response_contract@v1`、`runtime_budget@v1` 和 `tool_trajectory@v1`。没有响应质量标准的 Case 产生必填 `response_quality=UNAVAILABLE`，因此“请求可执行”不会被记成“业务质量通过”。显式评估器选择仍是覆盖语义。

新增而不破坏 V1 的门槛：

- `runtime.maxTotalTokens`：usage 已报告时约束总 token；未报告则 `UNAVAILABLE`。
- `tool.succeeded`：要求同名工具至少成功完成一次。
- `tool.sequence`：要求成功工具调用在 RuntimeEvent 序号上满足有序子序列。

`tool.called` 与 `tool.notCalled` 保持既有“是否发生调用”的语义，错误调用仍满足前者，不满足新 `tool.succeeded`。

### 可发现的 EvalSet 起点

`agentengine evalset init` 提供 `knowledge-qa`、`structured-output`、`tool-routing`、`service-sla` 四种本地模板；模板在写入前经过原生 parser 校验，默认拒绝覆盖已有文件。`evalset preview` 可验证规范化结果，不访问云端。

### 端到端验收

| Target | 真实验证 | 结果 |
| --- | --- | --- |
| Local Source | LangGraph 本地 Agent 产生 `knowledge_search -> cite_source` RuntimeEvent 工具轨迹 | `eval_868b3a9cbe9342e8bc8084d8db95ca5d` PASSED；参考答案、延迟、工具成功/顺序/期望工具均 PASS |
| Studio Build | 冻结构建产物执行，之后将可变源码改成抛错；经 Studio Operation 持久化报告和 evidence | `tests/studio/test_evaluation_build_target.py`：6 passed |
| A2A | 读取 `http://127.0.0.1:8808/.well-known/agent-card.json`，以 A2A 1.0 JSON-RPC 调用本地监听 Agent | `eval_32ac957d1f72491c9b04106d812a5581` PASSED；远端 Task 为 COMPLETED，参考答案和延迟均 PASS |
| 预发 `ar-20260707174402-be8182f1` | 使用 `--region pre-online` 的控制面查询及 CLI runtime 调用 | `hush-eval_agent` 为 RUNNING、1/1 ready；精确文本、约束文本、JSON 调用成功。Endpoint 未提供 A2A Card，尚不能由当前 EvalSet executor 生成报告 |

报告根目录为 `D:\agent\ksadk-python\tmp\evalset-v2-final-validation-20260820`。Evidence 的公开读取方式是 `EvidenceStore.read_trace(TraceRef)`；内部路径改用稳定哈希文件名，避免 Windows 长路径导致 Studio Operation 在报告落盘后失败。

下一阶段应新增 AgentEngine Runtime Target adapter：控制面解析固定 Endpoint/访问参数，chat runtime 执行 EvalSet，生成 TargetSnapshot/TargetRun/Report，并让 Studio 合并 CLI 的 report-only Run。工具参数/结果语义、成本、时延分段和分位数聚合属于后续评估器能力，不能用当前三个工具/预算门槛替代。
