## 工具说明

### 安全执行
- 需要执行 workspace 内脚本时，优先使用 `sh-safe` 或 `bash-safe`。
- `sh-safe` / `bash-safe` 只允许运行 workspace 内脚本，会拒绝 `-c` 这类高风险 shell 参数，并尽量清理继承环境变量。
- 在不需要原始网络能力时，公开网页检索与阅读优先使用 `web-safe`。

### 自我改进
- `.learnings/LEARNINGS.md` 用来记录纠正、最佳实践和认知缺口。
- `.learnings/ERRORS.md` 用来记录命令失败、集成故障和排障线索。
- `.learnings/FEATURE_REQUESTS.md` 用来记录用户提出但当前还不具备的能力。
- 某条经验反复出现时，把它压缩成短规则，再提升到 `AGENTS.md`、`SOUL.md` 或本文件。
