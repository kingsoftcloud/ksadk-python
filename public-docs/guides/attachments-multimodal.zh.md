# 附件与多模态输入

KsADK 支持文本、图片和文件输入。运行时会把上传内容归一化到当前 turn 的消息和附件
metadata 中，再交给框架 runner。

## 推荐做法

- 图片分析优先交给支持视觉的模型。
- 大文件不要直接塞进 prompt；先抽取摘要或结构化内容。
- 工具需要文件时使用 workspace 或附件引用，不猜测浏览器上传路径。
- 不把附件二进制写入长期记忆。

## 输入类型

常见协议形态包括：

- `input_text`
- `input_image`
- `input_file`
- 文件 metadata 和 workspace 引用

业务 Agent 应处理缺失、过期或不支持的附件引用，并给出明确错误。
