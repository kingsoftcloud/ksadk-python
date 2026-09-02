# Studio PluginHost 运行可观测性（用量上报）

## Purpose

定义 Studio PluginHost 链路（含经 DSH 注册的 KsADK Harness provider）中模型调用用量的上报契约：运行产生的用量事实必须进入 RuntimeEvent 流与运行记录，使 Studio 运行检查器与 API 消费者能看到真实 Token 消耗，而不是以 0 顶替。

## ADDED Requirements

### Requirement: 模型调用用量必须随推理轮次返回

Studio 绑定的 Harness Reasoner 在每次模型调用后 SHALL 返回该次调用的用量事实（input/output/cached/reasoning Token）；底层模型响应已提供用量时不得丢弃。

#### Scenario: 模型响应携带用量

- **WHEN** 模型客户端返回的响应包含非零 usage
- **THEN** Reasoner 返回的推理轮次携带等价的 input_tokens、output_tokens、cached_tokens、reasoning_tokens

#### Scenario: 模型响应缺失用量字段

- **WHEN** 上游响应不包含 usage
- **THEN** Reasoner 返回的用量为缺失态（而非伪造的 0 报告），由下游按 unavailable 处理

### Requirement: 用量事实必须到达运行记录与事件流

一次完成的 PluginHost 运行在推理轮次携带用量时，SHALL 发出 `usage.reported` 事件并在运行记录上落非零、`reported=true` 的用量（来源标注 pluginhost 链路）。

#### Scenario: 单轮回答的用量上报

- **WHEN** 一次会话运行完成且其模型调用返回了用量
- **THEN** 该运行的事件流包含 `usage.reported`，且运行记录的 input/output Token 与事件一致

#### Scenario: 运行检查器展示真实用量

- **WHEN** 用户在 Studio 运行检查器中查看上述运行
- **THEN** 总 Token 与输入/输出 Token 显示真实数值而非 0

### Requirement: 多轮工具循环用量必须累计

含工具调用的多轮推理 MUST 将各轮用量按字段累加后一次性上报，不丢失中间轮次。

#### Scenario: 两轮工具循环

- **WHEN** 运行经历"模型调用 → 工具执行 → 模型调用"两轮推理
- **THEN** `usage.reported` 的各 Token 字段等于两轮之和

### Requirement: 推理内容必须作为 reasoning 事实透出

模型返回推理内容（如 `reasoning_content`）且 Provider 链路使用该模型客户端时，每次推理轮次 SHALL 把非空推理文本作为 `item_kind="reasoning"` 的 RuntimeEvent 发出，使 Studio 既有投影将其呈现为 `thinking.*`；无推理内容时不得伪造空 reasoning 项。

#### Scenario: 推理模型返回思考内容

- **WHEN** 一次运行中模型返回非空推理文本
- **THEN** 运行事件流包含 `item_kind="reasoning"` 的事件且文本与模型返回一致，Studio 会话视图出现对应 `thinking.completed`

#### Scenario: 非推理模型

- **WHEN** 模型响应不携带推理内容
- **THEN** 事件流不出现 reasoning 项，会话视图无思考过程卡片
