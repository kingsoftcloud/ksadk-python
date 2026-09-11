# Agent Teams 本地联调

Teams 的领域服务来自启用的 DSH 插件，Studio 只挂载实际注册的 `studio.workspace.page` contribution。共享组件与协议来自同级 `ksadk-web`，此开发分支没有发布或提升公共包版本。

首次依赖安装后，请显式安装本地打包产物：

```sh
# 同级 ksadk-web 仓库先 npm ci
npm ci
npm run dev:local-web
npm run build
```

`dev:local-web` 构建同级 Web 库并安装临时 tgz，不修改 Studio 的公共版本声明及 lockfile。每次 `npm ci` 会恢复已发布依赖，需要重新执行该命令。后续正式集成应由发布流程在批准后替换为经过发布的共享版本。

前端验证：

```sh
npx tsc --noEmit
npm run test:ui
npm test
npx playwright install chromium
npm run test:e2e:teams
npm run test:teams:contract
```

浏览器测试运行真实 Studio App、generic workspace mount、共享 Teams 组件和 CSRF fetch；仅测试内替换 DSH contribution 及 HTTP 返回，所有合成数据在 `src/test/teamsFixtures.ts`。它验证界面及传输契约，不代表真实模型执行已经成功。

实际运行需启动工作区 Studio，在插件管理启用 Agent Teams，再由 DSH Core 注册团队入口。旧 `#/orchestration` 深链兼容进入 Teams 的启用页；Agent 配置、构建和历史会话保持原有入口。群快照订阅与成员既有 Run 观察使用独立 scope；关闭详情或停用插件只清理观察，不调用停止。群目标、定向消息、审批、验收与控制始终经 Groups API。

跨仓契约测试通过真实 Python TeamsDomain/TeamsRuntime 和受控 Host 生成创建群、首次目标、任务、审批与子执行投影，然后用已安装的共享 Web decoder、reducer 和 SSR 验证。它不会启动模型或修改实际工作区。

Performance checks are reproducible with `node scripts/test-teams-performance.mjs` and
`npx playwright test --config playwright.teams.config.mjs --grep 'performance reference'`.
The first checks a fixed 8-member / 200-node / 10,000-event data set against the actual Python domain contract and public Web reducer/SSR. The browser test records 20 samples each of send feedback and accepted SSE chunk-to-DOM latency, plus rendering 200 task nodes. Its transport is explicitly mocked; these figures do not measure model inference or live network latency. Reports are stored in ignored `output/` and Playwright attachments.

Member cancellation uses the complete member execution reference and a retry-stable idempotency key. A `cancel_requested` receipt leaves the real Run status unchanged until its terminal projection arrives. Historical execution inspectors do not expose the stop action. Shared artifact references are read from both snapshot-level immutable artifacts and task results, with authority and group checks on snapshots and events.
