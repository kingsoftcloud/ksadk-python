# G0.2 冻结稿修订提案:对齐 a2ui.org v1.0

> 状态:**提案,待拍板**。本文不擅自修改已冻结的 G0.2 schema,仅记录对齐 a2ui.org v1.0 官方协议的修订方案,供地基拍板。
> 依据:用户明确"支持 1.0 和后续协议就行"(2026-07-23)。
> 官方协议:a2ui.org v1.0 Candidate(2026-06-08,copyright Google 2025,repo github.com/a2ui-project/a2ui)。

---

## 0. 背景

G0.2 冻结稿(`docs/runtime-foundation-freeze-v1.md`)冻结的 A2UI 事件名(`a2ui.surface.begin/update/end`、`a2ui.interaction`、`a2ui.action`)是**早期草案记忆**命名,与 a2ui.org v1.0 官方协议**不一致**。本提案记录差异 + 对齐方案。

**关键分层**(来自 a2ui.org guide + ag-ui-protocol 源码核实,2026-07-23):
- **AG-UI** = 传输协议(`ag-ui-protocol` SDK,`from ag_ui.core import ...`):run/message/tool/activity 事件流。**成熟,最新 0.1.19**。
- **A2UI** = 声明式 UI 格式(surface/catalog/components):搭在 AG-UI 之上。**官方 Python 包(`a2ui-core`/`ag-ui-a2ui-toolkit`)仍停在 v0.9,v1.0(2026-06 Candidate)尚未进包**。
- 跨框架统一靠 AG-UI 中间件(`ag_ui_adk`/`copilotkit`/`ag_ui_langgraph`),agent 侧零侵入

**重要区分**:AG-UI 传输层(成熟)与 A2UI UI 层(仍 v0.9)是**两个协议**,切换时分别评估。我们自研的 `ksadk/a2ui/` 是 A2UI UI 层,与 `ag-ui-protocol` 传输层不冲突,与 `a2ui-core`(v0.9)才是同层竞品。

---

## 1. 差异对比

| 维度 | G0.2 冻结(现状) | a2ui.org v1.0(目标) |
|---|---|---|
| surface 创建 | `a2ui.surface.begin` | `createSurface` |
| surface 更新 | `a2ui.surface.update` | `updateComponents`(邻接表 flat list) |
| surface 删除 | `a2ui.surface.end` | `deleteSurface` |
| 交互/输入 | `a2ui.interaction` | `actionResponse`(同步回包) |
| 用户操作回传 | `a2ui.action` | `action` |
| 组件树 | 嵌套 children | 邻接表(adjacency list) |
| 数据模型 | `data_model` dict | JSON Pointer(RFC 6901)+ 双向绑定 |
| catalog | BASIC_CATALOG(Card/Text/Form/Select/RadioGroup/CheckboxGroup/ApprovalBar) | Basic Catalog(Text/Image/Button/TextField/CheckBox/Row/Column/List/Card/Tabs/Modal/Slider/ChoicePicker/DateTimeInput + 验证/格式化函数) |
| 传输 | 只 SSE | AG-UI/A2A/MCP/SSE/WebSocket/REST |
| 服务端调客户端函数 | 无 | `callFunction`(v1.0 新增) |

---

## 2. 修订方案(三阶段)

### 阶段 A:前端兼容层(已完成,2026-07-23)

`ksadk-web/src/utils/responses-stream.js` 已同时识别两套命名:
- ksadk 冻结:`a2ui.surface.begin/update/end` + `a2ui.interaction`
- 官方 v1.0:`createSurface`/`updateComponents`/`deleteSurface`

**不碰后端冻结 schema**,前端已对官方协议就绪。后端未来切官方命名,前端零改。

### 阶段 B:后端冻结稿修订(待拍板,本文档)

G0.2 冻结稿 v2:
1. 事件名改官方:`createSurface`/`updateComponents`/`deleteSurface`/`actionResponse`/`action`
2. 组件树改邻接表(flat list + parentId 引用)
3. 数据模型上 JSON Pointer(RFC 6901)
4. catalog 对齐 Basic Catalog(补 Text/Image/Button/TextField/Row/Column 等;ApprovalBar 是 ksadk 扩展,作 custom catalog 组件保留)
5. payload 必填键随之更新(`surfaceId` 而非 `surface_id`,camelCase 对齐官方)

**影响面**:G0.2 冻结稿 + `ksadk/a2ui/`(core/renderer/models)+ `ksadk/events/runtime_event.py` EventType + 所有消费方测试。需走冻结修订流程。

### 阶段 C:AG-UI 生态接入(长期,goal-18 之后)

- 后端:各框架经 AG-UI 中间件产 A2UI(`ag_ui_adk` for ADK、`CopilotKitMiddleware` for LangGraph、`ag_ui_strands` for Strands),替换自建 surface 产出
- 前端:用 `@copilotkit/a2ui-renderer` + `createCatalog` 替换手写 `A2UISurfaceView`,获得官方 Basic Catalog + BYOC
- 好处:零自维护协议、跨框架统一靠生态、agent 侧零侵入(CopilotKit 自动注入 `generate_a2ui` tool)

---

## 3. 建议

- **现在**:阶段 A 已完成(前端兼容),后端冻结 schema 不动(守 G0.2 冻结)。
- **goal-18 联调时**:用现有冻结命名跑通端到端(不阻塞验收)。
- **goal-18 之后**:启动阶段 B(冻结稿 v2 修订)+ 阶段 C(AG-UI 接入)。v1.0 仍是 Candidate,等转 Final 再全量对齐更稳。

---

## 4. 边界

- 本提案**不修改** G0.2 冻结 schema(元规则:冻结只能消费,改要回地基)。
- 阶段 A 前端兼容层是**加法**(多识别官方命名),不破坏现有冻结事件流。
- 预发 E2E(goal-18)仍依赖 pre-online 环境,与本提案无关。
