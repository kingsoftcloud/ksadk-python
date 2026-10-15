# 子调用取消扩展

`interrupt` 的可选 `scope_id` 指向出站 A2A 子调用；非空 `run_id` 必须同时提供。
可选 `client_token` 从 Server 的 `CancelRun.ClientToken` 传入，Server 负责命令去重。
未提供 `scope_id` 时沿用根运行取消语义。

Runtime 只有配置托管出站 A2A 和持久 invocation checkpoint 后才声明
`scoped_cancel: {"supported": true, "mode": "emulated"}`。旧 Runtime 缺少该声明时拒绝，
不能将子调用取消降级为根运行取消。SDK 按 checkpoint 的账号、租户、Runtime owner，
再校验 session、run 和 caller agent 归属，复用 InvocationService.cancel；忙时记录持久意图。
命令 accepted/duplicate 只代表接受请求，远端 confirmed/cancelled 由子调用规范 descriptor 表达。

这是冻结 `agent-kernel/v1` envelope 的可选扩展，原有 wire digest 保持不变；
新能力声明参与实际 Runtime capability digest。
