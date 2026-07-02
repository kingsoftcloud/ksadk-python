# 工作区文件

Workspace 用于保存 Agent 生成的文件产物，例如 HTML、Markdown、JSON、CSV 和代码。
本地 UI 与 hosted UI 都围绕同一个逻辑 workspace 展示、预览和下载文件。

## 使用场景

- 生成报告或发布说明。
- 生成前端 demo 文件。
- 保存结构化 JSON 或 CSV。
- 让用户在 UI 中预览和下载产物。

## 路径安全

业务代码必须把路径限制在 workspace 根目录内：

```python
from ksadk.sessions.local_service import resolve_local_session_dir

root = resolve_local_session_dir() / "workspace"
target = (root / "report.md").resolve()
if root.resolve() not in target.parents and target != root.resolve():
    raise ValueError("workspace path escapes workspace root")
```

## 环境变量

- `KSADK_WORKSPACE_FILES_ENABLED`
- `KSADK_WORKSPACE_ROOT_LABEL`
- `KSADK_WORKSPACE_MAX_UPLOAD_BYTES`

默认单文件上传上限为 100 MiB。
