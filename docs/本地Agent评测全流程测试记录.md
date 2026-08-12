# 本地 Agent 评测全流程测试记录

## 1. 结论

`agentengine eval --agent-dir` 已跑通 `LOCAL_SOURCE` 本地 Agent 评测。CLI 与 Studio UI 是两个入口，但最终汇合到同一套公共评测内核：

```text
CLI: agentengine eval -> cmd_eval -> execute_evaluation
UI:  React 评测页 -> POST /api/v1/evaluations -> Studio Operation -> execute_evaluation
                                      |
                                      v
EvalSet -> TargetAdapter -> TargetSnapshot -> EvalRunSpec
        -> TargetRun / RuntimeEvent Evidence -> Evaluator -> EvalRunReport
```

两者并非互相调用：本地 CLI 不请求 Studio HTTP 服务；Studio UI 也不启动 CLI 子进程。它们共享评测合同、TargetAdapter、Executor、Evaluator、Evidence 和报告模型，入口层的参数解析、Operation 管理和结果展示各自独立。

## 2. 当前目标类型

| Target | 用途 | 执行方式 |
|---|---|---|
| `LOCAL_SOURCE` | 开发中的本地源码快速评测 | CLI 或 Studio API 在当前进程内通过 `RuntimeExecutor` 执行 |
| `STUDIO_BUILD` | 对不可变 Studio Build 做正式回归 | Studio 解析 Build artifact，再通过规范化运行接口执行 |
| `A2A` | 评测远端 Agent | `A2ATargetAdapter` 通过 A2A HTTP 协议调用远端服务 |

`LOCAL_SOURCE` 不要求先启动 Studio。只有通过浏览器页面发起时才需要 Studio HTTP 服务；`A2A` 则要求目标 Agent 的 A2A 服务可访问。

## 3. 确定性本地 fixture

测试 fixture 位于 `tmp/local-agent-cli-e2e/`，仅用于本机验证，不纳入 Git：

```text
tmp/local-agent-cli-e2e/
|- agentengine.yaml
|- agent.py
`- suite.yaml
```

`agentengine.yaml` 声明 LangGraph 入口：

```yaml
name: cli-local-agent-e2e
framework: langgraph
entry_point: agent.py
agent_variable: graph
```

`agent.py` 不调用模型和网络，固定返回 `hello from real agentengine cli`；`suite.yaml` 使用 `response.contains` 断言，保证验证结果可重复。

## 4. CLI 全流程

在仓库根目录执行：

```powershell
git fetch origin agentkit-eval-phase1 --prune

agentengine.exe eval `
  --evalset-file D:\agent\ksadk-python\tmp\local-agent-cli-e2e\suite.yaml `
  --agent-dir D:\agent\ksadk-python\tmp\local-agent-cli-e2e `
  --report-dir D:\agent\ksadk-python\tmp\local-agent-cli-e2e\reports-20260812 `
  --format json
```

已验证结果：

```text
CLI exit code: 0
Run ID: eval_83b9b9b39e66431db17cbaedfbb5135f
EvalRun status: PASSED
Target kind: local_source
Runtime: langgraph
Case: hello
Case output: hello from real agentengine cli
Metric: response_contract / PASS
TraceRef: seq 2..5
```

报告目录包含：

```text
report.json
evidence/<session-id>/<invocation-id>.json
```

报告中保存了源码摘要、框架、入口、Git HEAD/dirty 状态；Evidence 保存同一 invocation 的 `run.started`、`text.completed`、`run.completed` 等 `RuntimeEvent`，报告的 `TraceRef.seqStart/seqEnd` 与事件范围一致。

## 5. 启动 Studio 后执行 CLI

启动 React Studio：

```powershell
agentengine.exe studio `
  -p 8893 `
  --no-open `
  D:\agent\ksadk-python
```

保持服务运行，再执行：

```powershell
agentengine.exe eval `
  --evalset-file D:\agent\ksadk-python\tmp\local-agent-cli-e2e\suite.yaml `
  --agent-dir D:\agent\ksadk-python\tmp\local-agent-cli-e2e `
  --report-dir D:\agent\ksadk-python\tmp\local-agent-cli-e2e\reports-with-studio-8893 `
  --format json
```

历史实测结果仍为 `PASSED`，且 Studio 服务日志没有新增 HTTP 请求。该结果证明 `--agent-dir` 始终在 CLI 进程内执行；先启动 Studio 不会把它隐式改成 HTTP 调用。

## 6. Studio UI/API 全流程

浏览器打开 `http://127.0.0.1:8893/#/evaluations`。React 页面仅在评测路由挂载后请求：

```text
GET  /api/v1/evaluations
GET  /api/v1/evaluation-targets
POST /api/v1/evaluations
GET  /api/v1/operations/{operation-id}
GET  /api/v1/evaluations/{evaluation-id}
```

其中：

1. `GET /api/v1/evaluation-targets` 返回可发现的 EvalSet 与成功 Build。
2. `POST /api/v1/evaluations` 创建后台 Operation。
3. `StudioService.submit_public_evaluation()` 规范化 Target，并调用 `execute_evaluation()`。
4. 前端轮询 Operation，成功后刷新报告列表和 Case 详情。

CLI 和 UI 的公共汇合点是 `execute_evaluation()`，不是 HTTP 层。两种入口可以选择不同 Target，因此“同一链路”是指公共评测内核和报告合同相同，不代表调用路径逐行完全一致。

本轮在临时端口 `8894` 做了真实 HTTP smoke：

```text
GET /                                      -> 200，React root 与新 bundle 正常
GET /static/assets/index-BlUUr8h_.js       -> 200，包含评测 route/API
GET /api/v1/system/bootstrap               -> 200，features.evaluation=true
GET /api/v1/evaluation-targets             -> 200，BuildCount=0，EvalSetCount=0
```

Studio 开启本地会话保护时，首次 `GET /` 会设置 `agentkit_studio_session` HttpOnly cookie；后续 API 请求必须携带该 cookie。直接绕过首页请求受保护 API 返回 `LOCAL_SESSION_REQUIRED` 属于预期安全行为。验证后已停止临时 `8894` 服务。

## 7. 不影响既有 Studio 页面

React Studio 是唯一前端基线。评测前端源码限定在：

```text
ksadk/studio/react-ui/src/pages/EvaluationsPage.tsx
ksadk/studio/react-ui/src/pages/evaluations.css
```

共享文件只做最小接线：

```text
ksadk/studio/react-ui/src/App.tsx
ksadk/studio/react-ui/src/components/NavigationRail.tsx
```

隔离原则：

1. 旧页面组件和全局样式不承载评测逻辑。
2. 评测样式全部使用 `.evaluation-page__*` 命名空间。
3. 只有 `#/evaluations` 挂载 `EvaluationsPage`，旧页面不会请求评测 API。
4. 不恢复已删除的 `ksadk/studio/web`、旧 `static/app.js` 或旧评测 JS/CSS。
5. `ksadk/studio/static/` 只保存 React 构建产物。

## 8. 自动化覆盖

评测内核专项：

```powershell
python -m pytest `
  tests/test_evaluation_contracts.py `
  tests/test_evaluation_target.py `
  tests/test_evaluation_local_adapter.py `
  tests/test_evaluation_evidence.py `
  tests/test_evaluation_evaluators.py `
  tests/test_evaluation_executor.py `
  tests/test_cmd_eval.py `
  tests/studio/test_evaluation_shell.py `
  tests/studio/test_evaluation_build_target.py `
  -q --basetemp=tmp/pytest-final-eval
```

Studio 既有页面/API 回归：

```powershell
python -m pytest `
  tests/studio/test_api.py `
  tests/studio/test_codex_api.py `
  tests/studio/test_codex_static.py `
  -q --basetemp=tmp/pytest-studio-regression
```

React 回归与构建：

```powershell
Set-Location ksadk/studio/react-ui
npm.cmd run test
npm.cmd run test:ui -- --maxWorkers=1
npm.cmd run build
```

Windows 上并行执行 Vitest 和 Vite build 曾触发 `VirtualAlloc` 失败，因此这里固定顺序执行，并把 Vitest worker 限制为 1。Python 项目的 `.venv` 当前未安装 pytest，`uv run` 在本机更新 wheel 的 PE resource 时失败，专项回归暂用全局 Python 3.13；这属于验证环境限制，不是测试断言失败。

## 9. 尚未覆盖

- 真实远端 A2A Agent 的网络 E2E，需要独立 A2A 服务和可用 endpoint。
- 有真实模型调用的非确定性本地 Agent 评测，需要配置凭据、超时和数据外发策略。
- 多副本部署下的 session/evidence 持久化；本地 backend 只适用于单机调试，Kubernetes 多副本应使用 PostgreSQL。
