# LangGraph 模型输出可见性

Agent 可以在单次模型调用的 RunnableConfig.metadata 中设置
`ksadk_output_visibility: "internal"`，表示该调用用于内部选择或编排，
其正文和推理文本不属于用户回答。此字段由 Agent 声明，由
`LangGraphRunner.stream` 解释；不是 Server 请求字段，不需要网关处理。

仅在需要隐藏的模型调用上合并 metadata，保留原 config 和 callbacks，
不要修改共享 config 或在整个图上设置该标记，以免后续回答继承它。
未标记的调用保持原有行为。该字段控制模型流文本，不是通用数据访问控制。

Runner 根据模型事件 metadata 识别标记，并按 run_id 记住可见性，
以支持后续流事件不含 metadata 的情况。内部正文和推理文本不进入
共享解析器、用户 text/thinking 事件或最终累计正文；usage、首 token
时间、模型生命周期及原始追踪仍保留。工具和 custom 事件的行为不变。

图仍应明确生成最终答案或错误输出。最终封装及结果后缀沿用原协议；
本约定不改变图最终输出与累计文本的优先级，也不做字符串去重。

兼容要求：Agent 和消费该标记的 KsADK 必须一起部署。旧 Runner 会忽略
此标记，不能仅更新 Agent 后宣称输出已隔离。无需因此变更 Server、
模型网关或评估器；其他模式的接口扩展属于独立改动。

验证覆盖事件回放和真实 LangGraph：内部选择与回答产生相同或不同文本、
推理片段隔离、metadata 传播、未标记行为、usage 和模型生命周期保留，
以及正式答案和 custom 结果后缀各输出一次。
