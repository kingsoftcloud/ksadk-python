---
name: agentengine-cluster-debug
description: Use when troubleshooting AgentEngine 管理集群、算力集群或 runtime pod 问题，尤其是在本地正常但部署后失败、需要从 `agent_id` 追到实际 runtime/cluster/pod、或 KB/LTM/AICP 仅在远端 runtime 中超时、报错、无响应时。
---

# AgentEngine 集群排障

## 概述

这个 skill 只用于 AgentEngine 远端排障，不是通用 Kubernetes 手册。

先守住这三个原则：

1. 先分清问题在管理集群还是算力集群。
2. 先拿到真实的 `compute cluster id`、namespace、pod、env，再判断是否需要改代码。
3. 把问题归类到代码、配置、资源 ID、依赖、网络/基础设施其中一类，不要混着猜。

绝大多数“本地正常、远端异常”都不是模型本身问题，常见根因是：

- runtime 环境变量和本地不一致
- KB/LTM 资源 ID 配错、过期或租户范围不对
- 镜像里缺依赖
- runtime pod 无法访问内网依赖
- AK/SK、地域、inner/public endpoint 组合错误

## 何时使用

出现以下情况时使用：

- `agentengine agent invoke` 部署后失败，但本地运行正常
- KB/LTM 本地可用，serverless runtime 中超时、报错或无响应
- 出现 `InnerAccountCanOnlyAccessThroughIntranet`
- 出现 `Dataset not found`、`NotFound`、资源不存在
- runtime pod 看起来已创建，但普通对话卡住或没有输出
- 需要从 `agent_id` 找到实际落到哪个算力集群、哪个 namespace、哪个 pod
- 需要进入 runtime pod 核对 env、依赖、DNS、TCP、HTTP 或最小 SDK 调用

以下情况不要用这个 skill：

- 已经能在本地稳定复现的纯业务逻辑问题
- 与管理/算力/runtime 完全无关的 SDK 设计问题

## 最短排查路径

除非已经有更强信号，否则按这个顺序：

1. 先确认环境：`pre` 或 `online`。
2. 在管理集群确认 runtime 是否真的创建、状态是否异常。
3. 从管理侧拿到当前生效的 `compute cluster id`。
4. 判断目标算力集群是 `kce1.0` 还是 `kce2.0`。
5. `kce1.0` 才在本地取 kubeconfig；`kce2.0` 需要进入管理集群内的 pod 再连。
6. 找到 runtime 对应 namespace 和 pod。
7. 看日志、env、依赖，再做 DNS/TCP/HTTP 探测。
8. 只记录第一个明确失败层，不要一次改多处。

## 管理集群

已知 kubeconfig：

- 预发：`~/.kube/agentengine-pre`
- 线上：`~/.kube/agentengine-online`

管理集群上部署着至少这些服务：

- `agentengine-server`
- `agent-runtime-service`

服务端辅助命令参考：

- `/Users/xiayu/kingsoft/code/agent-sdk/agentengine-server/Makefile`

常用入口：

- `make status ENV=pre`
- `make logs-tail ENV=pre`
- `make debug ENV=pre`
- `make shell ENV=pre`

管理集群负责回答这些问题：

- control plane 是否健康
- runtime 是否被创建
- 当前 runtime 状态是什么
- 当前生效的 `compute cluster id` 是什么
- 目标算力集群是 `kce1.0` 还是 `kce2.0`

注意：管理集群健康，不代表 runtime pod 健康。

## 如何从 agent_id 找 compute cluster id

先不要假设本地就能直接连到算力集群。

正确做法是：

1. 在管理集群查询该 `agent_id` 对应 runtime 的当前状态和生效配置。
2. 优先按当前服务端查询入口、现网脚本或管理侧日志确认 `compute cluster id`。
3. 如果需要进一步确认算力集群地址和配置，可在管理集群内的 `agent-runtime-service` 查询当前生效记录。

这里不要编造数据库表名、字段名或内部接口名。按当前服务端查询入口或现网脚本确认。

相关服务端代码入口：

- `/Users/xiayu/kingsoft/code/agent-sdk/agent-runtime-service/api/service/agents/create_agent_runtime.go`
- `/Users/xiayu/kingsoft/code/agent-sdk/agent-runtime-service/controller/agentctl/agent_runtime.go`

## 算力集群访问前提

获取算力集群 kubeconfig 的工具是：

- `/Users/xiayu/kingsoft/code/agent-sdk/get-kubeconfig/main.go`

但先看集群类型：

- `kce1.0`：本地可直接使用 `get-kubeconfig` 获取并连接
- `kce2.0`：本地通常不能直接连，需进入管理集群内部 pod 后再访问

如果目标是 `kce2.0`，优先进入以下 pod 再继续排查：

- `agentengine-server`
- `agent-runtime-service`

这两类 pod 同时也是确认现网查询入口、脚本和当前生效算力配置的落脚点。

## 获取算力 kubeconfig

已拿到 `compute cluster id` 且确认目标可直接访问时，使用：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/get-kubeconfig
go run . \
  --cluster-id <compute-cluster-id> \
  --region cn-beijing-6 \
  --kubeconfig ~/.kube/config-<compute-cluster-id> \
  --set-default=false
```

如果目标是 `kce2.0`，不要在本地反复尝试超时连接。应先切到管理集群内 pod 再执行后续查询或连通性检查。

## 如何定位 runtime pod

拿到算力集群访问能力后，重点检查：

- namespace，通常与 runtime 或 `agent_id` 对应
- deployment / pod 状态
- restart 次数
- recent events
- `agent-runtime` 容器日志

如果 namespace 或 pod 名无法直接推出，按管理侧查询结果或现网脚本确认，不要凭命名规则硬猜。

## Pod 内检查顺序

进入 runtime pod 后，按这个顺序：

1. 日志
2. 生效环境变量
3. 依赖是否安装
4. DNS 是否正常
5. TCP 是否可达
6. 最小 HTTP/SDK 请求

### 日志

先看 `kubectl logs`。如果当前无输出，再看：

- `--previous`
- pod events
- 最近是否重启

### 环境变量

重点核对：

- `KSADK_KB_*`
- `KSADK_LTM_*`
- `KSYUN_REGION`
- AK/SK 相关 env
- endpoint、scheme、开关类配置

本地 `.env` 正确，不代表 runtime 里生效值正确。
核对 AK/SK 时只确认“是否存在、来源是否正确、是否走到了预期变量名”，不要把明文值直接打印到终端、录屏或日志里。

### 依赖

如果远端报云 SDK 缺失、导入失败或接口不存在，不要猜，直接在 pod 里验证 import。

远端失败、本地正常，优先归到镜像打包或依赖声明问题。

### DNS / TCP / HTTP

对 AICP 内网访问，DNS 成功不代表真正可用，至少要继续做 TCP 检查。

已验证过的目标域名模式：

- `aicp.inner.api.ksyun.com`

判断原则：

- DNS 失败：pod DNS、网络策略或名字解析问题
- DNS 成功但 TCP `80/443` 不通：路由、ACL、安全组、egress 或基础设施链路问题
- TCP 可通但请求仍失败：更像 endpoint、鉴权、资源 ID、协议或请求内容问题

### 最小 SDK / HTTP 请求

只在 env 和网络都确认后再做。

推荐使用项目现有检查脚本；如果没有，则写一次性最小 Python 片段，至少打印：

- endpoint
- scheme
- region
- resource id
- 异常类型
- 异常消息

不要凭空假设 pod 内一定存在某个脚本名；按当前镜像内容、服务端查询入口或现网脚本确认。

## 故障归类

按下面方式收敛：

- `InnerAccountCanOnlyAccessThroughIntranet`
  - 多数是内外网 endpoint 用错，或账号要求走内网
  - 先确认 runtime 配置的是 inner 域名
  - 再确认 runtime pod 到 inner 域名的 TCP 真的可达

- `Dataset not found`
  - 多数是 KB 数据集 ID、支持库 ID、租户范围或环境不对
  - 这类问题通常不是改网络能解决

- memory namespace / index 相关 `NotFound`
  - 多数是 namespace、index 或租户范围错误

- 普通对话卡住、迟迟无首包
  - 常见是请求开始阶段就阻塞在 KB/LTM 预加载或远端依赖访问
  - 先看是否每次请求都同步查 KB/LTM
  - 再看远端网络超时与依赖调用日志

- 本地正常、远端提示缺包
  - 优先归类为镜像/依赖问题

## 升级前需要收集的信息

如果最终判断更像平台或基础设施问题，至少带上这些事实：

- `agent_id`
- 环境：`pre` 或 `online`
- `compute cluster id`
- 集群类型：`kce1.0` 或 `kce2.0`
- namespace
- pod 名
- 目标域名和端口
- DNS 结果
- TCP 结果
- 最小报错信息和时间点
- 本地是否可复现

不要只带一句“远端不通”就升级。

## 参考命令

具体命令模板见 [`references/commands.md`](./references/commands.md)。
