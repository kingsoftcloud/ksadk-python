# Hermes运行时说明

`deploy/hermes/` 保存当前共享 Hermes runtime 镜像的事实约定与运行时资产。

## 1. 目录内容

- `Dockerfile`
- `entrypoint.sh`
- `runtime/app.py`
- `skills/`
- `agentengine.yaml.template`

## 2. 当前镜像构建方式

Hermes 镜像当前直接从仓根构建，并在构建期复制同仓共享源码：

- `COPY ksadk_runtime_common /opt/ksadk_runtime_common`
- `PYTHONPATH=/opt`

这意味着：

- 不依赖独立 common wheel
- Hermes runtime 与本地 runtime 使用同一份 workspace files 源码

## 3. 默认目录约定

Hermes runtime 默认把所有可变状态集中到 `~/.hermes` 这个 single persistent directory。

```text
HOME=/home/node
HERMES_HOME=/home/node/.hermes
HERMES_WORKDIR=/home/node/.hermes/workspace
KSADK_WORKSPACE_ROOT=/home/node/.hermes/workspace
AGENT_BROWSER_HOME=/usr/local/lib/node_modules/agent-browser
AGENT_BROWSER_STATE_DIR=/home/node/.hermes/browser
AGENT_BROWSER_SOCKET_DIR=/home/node/.hermes/browser/run
```

建议默认把持久化存储直接挂到：

```text
/home/node/.hermes
```

## 4. 对外服务面

Hermes runtime 当前同时提供：

- `/`：dashboard Web UI
- `/v1/*`：OpenAI 兼容 API
- `/_ksadk/terminal/ws`：远端终端
- `/_ksadk/workspace/v1/*`：workspace files 数据面
- `/health`：wrapper 健康检查

## 5. Hosted 行为

当前 Hosted Hermes 的运行方式是“容器内托管进程”，不是桌面守护进程。

- `entrypoint.sh` 自动拉起 gateway
- 容器内会做本地重启
- 重启预算耗尽后交给 Kubernetes 重建 pod

## 6. 模型与环境变量处理

当前实现中：

- 如果 `OPENAI_BASE_URL` 指向公共 `kspmas.ksyun.com`，会在云端改写为可达的内部地址
- `glm-5.1` 会自动补 `HERMES_CONTEXT_LENGTH=200000`
- KSPMAS / `glm-5.1` 部署默认 fallback 到 `kimi-k2.6`

## 7. workspace files 集成

Hermes runtime 直接挂载 `create_workspace_files_router(...)`，并把根目录绑定到：

```text
/home/node/.hermes/workspace
```

因此：

- `agentengine files`
- Hosted UI 文件面板
- `agentengine agent invoke --local-workspace`

都能复用同一份远端工作区数据面。

## 8. 典型构建命令

默认不需要本地构建镜像，直接部署平台预置 Hermes runtime：

```bash
agentengine hermes deploy
```

在仓库根目录执行：

```bash
make hermes-build
make hermes-push HERMES_TAG=2026.4.30
make hermes-size
```

## 9. 相关文档

- [ksadk使用文档](../../docs/ksadk使用文档.md)
- [ksadk技术设计](../../docs/ksadk技术设计.md)
- [工作区文件技术设计](../../docs/工作区文件技术设计.md)
