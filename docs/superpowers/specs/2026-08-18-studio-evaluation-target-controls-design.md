# Studio Evaluation Target Controls Design

## Goal

简化 Studio 评测表单：用户不再手工输入云端 Dataset version；Target locator 按 Target 类型呈现为业务控件，同时保持后端 `TargetRef.locator` 和固定 Dataset version 追溯合同不变。

## Design

- 云端 Dataset 通过 `Cloud Dataset` 下拉项选择固定快照，选项显示 Dataset 名称和 `vN`。移除独立的数字版本输入框，避免用户提交不在 Catalog 中的 Dataset/version 组合。
- `A2A Agent` 显示“Agent 地址” URL 输入框。
- `本地源码` 显示“Agent 源码目录”输入框，值仍映射为内部 `target.locator`。
- `Studio Build` 继续显示 Build 下拉框，值为不可变 Build ID。
- POST `/api/v1/evaluations` 的 `target.kind/locator` 与 `cloudDataset.version` payload 不变；版本仍写入 EvalRunSpec/Report。

## Verification

- React 页面测试覆盖：版本输入不存在、历史版本仍可从下拉选择、三类 Target 的业务标签和 payload。
- 运行 React UI 测试和构建；运行 KsADK 受影响 Studio 测试。
- agent-eval 仅提交本轮已验证的 Dataset catalog 合同修复，不修改其 API 行为。
