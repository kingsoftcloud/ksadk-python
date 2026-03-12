# OpenClaw 服务端 API 接入文档（按 ksadk 代码对齐）

本文档基于 `ksadk-python` 当前实现（`ksadk/api/client.py`、`ksadk/cli/cmd_openclaw.py`、`ksadk/cli/cmd_dashboard.py`）整理。
重点修正了原文中与真实调用不一致的入参/出参描述，并补齐全部接口的 cURL 示例。

## 1. 基础规范

### 1.1 接口地址

- 公网: `https://aicp.api.ksyun.com`
- 内网: `http://aicp.inner.api.ksyun.com`

KOP 模式下，Action 通过 Query 传入：

- `POST /?Action=CreateAgentProduct&Version=2024-06-12`

非 KOP 模式可走路径：

- `POST /agentengine/api/v1/CreateAgentProduct`

### 1.2 公共请求 Header

| Header 名 | 必填 | 说明 |
| :--- | :--- | :--- |
| `Content-Type: application/json` | 是 | JSON 请求体 |
| `Accept: application/json` | 是 | JSON 响应 |
| `Host` | 是 | 参与签名 |
| `X-Ksc-Request-Id` | 建议 | 请求追踪 ID |
| `X-Ksc-Region` | 是 | 控制面 Region，默认 `cn-beijing-6` |
| `X-Ksc-Source` | 否 | SDK 默认 `ksadk-cli` |
| `X-Ksc-Account-Id` | 条件 | 多租户场景建议传 |
| `Authorization` | 生产建议 | AWS V4 签名 |
| `X-Action` / `X-Version` | KOP 可选 | SDK 在 KOP 模式会自动补齐 |

### 1.3 返回体约定（重要）

网关原始返回通常是：

```json
{
  "Code": 0,
  "Message": "Success",
  "Data": { "...": "..." },
  "RequestId": "..."
}
```

`ksadk.api.client.AgentEngineClient._action()` 的行为：

1. `Code != 0` 直接抛异常。
2. 默认只返回 `Data`（若 `Data` 为空才返回整个对象）。
3. 自动把字段从 PascalCase 转为 snake_case。

因此，SDK 用户看到的返回字段名与网关原始字段名不同。

### 1.4 cURL 通用变量（示例）

```bash
export BASE_URL="https://aicp.api.ksyun.com"
export VERSION="2024-06-12"
export REGION="cn-beijing-6"
export AUTH="AWS4-HMAC-SHA256 Credential=xxx/20260311/${REGION}/aicp/aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature=xxx"
```

## 2. Agent 实例生命周期管理

## 2.1 创建实例（CreateAgentProduct）

### 请求体（网关字段）

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `Name` | 是 | 实例名称 |
| `Description` | 否 | 实例描述 |
| `Framework` | 是 | OpenClaw 场景固定 `openclaw` |
| `DeploymentType` | 是 | `Code` 或 `Container` |
| `Region` | 否 | 部署地域，默认 `cn-beijing-6` |
| `Resource.Cpu` | 是 | CPU 核数（整数） |
| `Resource.Memory` | 是 | 内存 GiB（整数） |
| `Scaling.MinReplicas` | 是 | 最小副本 |
| `Scaling.MaxReplicas` | 是 | 最大副本 |
| `Scaling.QpsPerInstance` | 是 | 单实例并发阈值（创建时字段名） |
| `AutoPay` | 是 | `true`（CreateAgentProduct 订单链路） |
| `Access.AuthType` | 否 | 如 `ApiKey` / `None` |
| `Access.IamRole` | 条件 | 传 `Access` 时可带 |
| `ContainerConfig.*` | 条件 | `DeploymentType=Container` 时使用 |
| `CodeConfig.*` | 条件 | `DeploymentType=Code` 时使用 |
| `Advanced.EnableObservability` | SDK 默认传 | 默认 `true` |
| `Advanced.EnvironmentVariables[]` | 否 | 环境变量，元素为 `{Key, Value, IsSensitive}` |
| `Advanced.InboundIdentityAuth` | 否 | 入站身份鉴权 |
| `Advanced.ProjectId` | 否 | 项目归属 |

`ContainerConfig` 在 SDK 里实际会构造：

- `ImageType`（默认 `Personal`）
- `NameSpace`、`ImageRepo`、`ImageVersion`、`ImageAddr`
- `UserName`/`Password`（仅当两者都提供）

### 响应体（SDK 侧常见字段）

| 字段（snake_case） | 说明 |
| :--- | :--- |
| `agent_id` | 可能直接返回实例 ID |
| `order_id` | 常见于创单异步流程（先有订单，后有实例） |
| `name` | 实例名 |
| `endpoint` | 访问地址（可能稍后才可用） |
| `api_key` | 平台鉴权密钥（仅部分场景返回） |

说明：OpenClaw CLI 对 `CreateAgentProduct` 的兼容逻辑是“优先读 `agent_id`，若仅返回 `order_id` 则轮询 `GetAgent` 直到拿到 `agent_id`”。

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=CreateAgentProduct&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "Name": "openclaw-demo",
    "Description": "OpenClaw demo",
    "Framework": "openclaw",
    "DeploymentType": "Container",
    "Region": "cn-beijing-6",
    "Resource": {
      "Cpu": 2,
      "Memory": 4
    },
    "Scaling": {
      "MinReplicas": 1,
      "MaxReplicas": 3,
      "QpsPerInstance": 20
    },
    "AutoPay": true,
    "Access": {
      "AuthType": "None",
      "IamRole": "KsyunAgentEngineDefaultRole"
    },
    "ContainerConfig": {
      "ImageType": "Personal",
      "NameSpace": "agentengine-public",
      "ImageRepo": "openclaw",
      "ImageVersion": "latest",
      "ImageAddr": "hub.kce.ksyun.com/agentengine-public/openclaw:latest"
    },
    "Advanced": {
      "EnableObservability": true,
      "EnvironmentVariables": [
        {"Key": "OPENCLAW_MODEL_API_KEY", "Value": "sk-xxx", "IsSensitive": false},
        {"Key": "OPENCLAW_GATEWAY_AUTH_MODE", "Value": "trusted-proxy", "IsSensitive": false}
      ],
      "InboundIdentityAuth": "None"
    }
  }'
```

## 2.2 查询实例详情（GetAgent）

### 请求体（网关字段）

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `AgentId` | 二选一 | 实例 ID |
| `Name` | 二选一 | 实例名称 |
| `IncludeApiKey` | 否 | `true` 时请求返回 `api_key`（若后端支持） |
| `Id` | 兼容字段 | SDK 在老控制面兼容回退时会尝试 |

原文“`AgentId` 必填”不准确，代码明确支持按 `Name` 查询。

### 响应体（SDK 侧常见结构）

| 字段（snake_case） | 说明 |
| :--- | :--- |
| `basic.agent_id` | 实例 ID |
| `basic.name` | 名称 |
| `basic.description` | 描述 |
| `basic.framework` | 框架 |
| `basic.region` | 地域 |
| `basic.status` | 状态 |
| `basic.phase` | 阶段 |
| `basic.replicas` / `basic.ready_replicas` | 副本信息 |
| `basic.created_at` / `basic.updated_at` | 时间 |
| `quick_access.public_endpoint` / `quick_access.private_endpoint` | 访问地址 |
| `quick_access.api_key` | API Key（条件返回） |
| `deployment.artifact_path` | 当前制品路径 |
| `advanced.observability_url` | 可观测地址（若配置） |

### cURL 示例（按 AgentId）

```bash
curl -X POST "${BASE_URL}/?Action=GetAgent&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "AgentId": "ar-xxxxxxxx",
    "IncludeApiKey": true
  }'
```

### cURL 示例（按 Name）

```bash
curl -X POST "${BASE_URL}/?Action=GetAgent&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "Name": "openclaw-demo",
    "IncludeApiKey": true
  }'
```

## 2.3 更新实例配置（UpdateAgent）

### 请求体（网关字段）

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `AgentId` | 是 | 实例 ID |
| `Description` | 否 | 描述 |
| `Resource.Cpu` / `Resource.Memory` | 否 | 资源更新 |
| `Scaling.MinReplicas` / `Scaling.MaxReplicas` | 否 | 弹缩策略 |
| `Scaling.Concurrency` | 否 | 并发阈值（更新时字段名） |
| `ContainerConfig.*` | 条件 | 更新容器镜像 |
| `CodeConfig.*` | 条件 | 更新代码制品 |
| `EnvironmentVariables[]` | 否 | 顶层字段（注意不是 `Advanced.EnvironmentVariables`） |
| `Access.AuthType` / `Access.IamRole` | 否 | 访问控制 |
| `Advanced.InboundIdentityAuth` | 否 | 入站身份鉴权 |
| `Advanced.ProjectId` | 否 | 项目归属 |

原文仅列了 `Resource` 与 `Scaling`，漏掉了镜像更新、环境变量、鉴权和高级配置字段。

### 响应体（SDK 侧）

SDK 直接返回 `Data` 的 snake_case 结果；常见会包含更新后的部分详情（如 `endpoint`、`name` 等），字段集合以后端返回为准。

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=UpdateAgent&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "AgentId": "ar-xxxxxxxx",
    "Description": "updated by api",
    "ContainerConfig": {
      "ImageType": "Personal",
      "NameSpace": "agentengine-public",
      "ImageRepo": "openclaw",
      "ImageVersion": "v2",
      "ImageAddr": "hub.kce.ksyun.com/agentengine-public/openclaw:v2"
    },
    "Resource": {
      "Cpu": 4,
      "Memory": 8
    },
    "Scaling": {
      "MinReplicas": 1,
      "MaxReplicas": 5,
      "Concurrency": 30
    },
    "EnvironmentVariables": [
      {"Key": "OPENCLAW_MODEL_API_KEY", "Value": "sk-yyy", "IsSensitive": true}
    ],
    "Access": {
      "AuthType": "None",
      "IamRole": "KsyunAgentEngineDefaultRole"
    },
    "Advanced": {
      "InboundIdentityAuth": "None",
      "ProjectId": "proj-demo"
    }
  }'
```

## 2.4 删除实例（DeleteAgent）

### 请求体

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `AgentId` | 是 | 要删除的实例 ID |

### 响应体

网关原始层通常为 `Code/Message` 成功语义；SDK 的 `delete_agent()` 以“未抛异常”为成功并返回 `true`。

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=DeleteAgent&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "AgentId": "ar-xxxxxxxx"
  }'
```

## 2.5 列表查询（ListAgents）

### 请求体（SDK 当前实际发送）

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `Region` | 是 | 当前 SDK 默认发送该字段 |

说明：原文中的 `Page/Size` 在网关可能支持，但 `ksadk` 当前 `list_agents()` 封装未传这两个字段。

### 响应体（SDK 侧常见字段）

| 字段（snake_case） | 说明 |
| :--- | :--- |
| `agents[]` | 实例数组 |
| `agents[].agent_id` | 实例 ID |
| `agents[].name` | 名称 |
| `agents[].framework` | 框架 |
| `agents[].status` | 状态 |
| `agents[].replicas` / `agents[].ready_replicas` | 副本数 |
| `agents[].endpoint` | 地址 |
| `agents[].region` | 地域 |
| `total` | 总数（是否返回取决于后端） |

OpenClaw 场景通常需要 `framework == "openclaw"` 过滤。

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=ListAgents&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "Region": "cn-beijing-6"
  }'
```

## 3. Dashboard 免密访问

当前 OpenClaw 仅保留 `trusted-proxy + CreateDashboardAccessLink` 链路。

## 3.1 创建短链接（CreateDashboardAccessLink）

### 请求体（网关字段）

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `AgentId` | 二选一 | 目标实例 ID |
| `Name` | 二选一 | 目标实例名称 |
| `LinkType` | 否 | `private`（默认）或 `share` |
| `Path` | 否 | UI 路径，默认 `/` |
| `ExpiresSeconds` | 否 | 过期秒数；`share` 支持 `0` 表示长期 |

`agentengine dashboard` 侧的参数校验（CLI 真实行为）：

- `private`: `30~3600`
- `share`: `0` 或 `300~2592000`

### 响应体（SDK 侧常见字段）

| 字段（snake_case） | 说明 |
| :--- | :--- |
| `link_id` | 短链接 ID |
| `access_url` | 可直接打开的 URL（CLI 强依赖此字段） |
| `expires_at` | 过期时间（可空） |
| `link_type` | 链接类型 |
| `path` | 路径 |
| `status` | 状态 |

原文“`AccessUrl` 有时返回”与当前 CLI 行为冲突。当前 CLI 逻辑是：缺少 `access_url` 视为失败。

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=CreateDashboardAccessLink&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "AgentId": "ar-xxxxxxxx",
    "LinkType": "share",
    "Path": "/",
    "ExpiresSeconds": 3600
  }'
```

## 3.2 枚举短链接（ListDashboardAccessLinks）

### 请求体（网关字段）

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `AgentId` | 二选一 | 目标实例 ID |
| `Name` | 二选一 | 目标实例名称 |
| `LinkType` | 否 | 类型过滤（`private`/`share`） |
| `Status` | 否 | 状态过滤（如 `active`/`revoked`） |
| `Page` | 否 | 默认 `1` |
| `Size` | 否 | 默认 `20` |

### 响应体（SDK 侧常见字段）

| 字段（snake_case） | 说明 |
| :--- | :--- |
| `total` | 总数量 |
| `links[]` | 链接列表 |
| `links[].link_id` | 链接 ID |
| `links[].link_type` | 类型 |
| `links[].status` | 状态 |
| `links[].path` | 路径 |
| `links[].expires_at` | 过期时间 |
| `links[].created_at` | 创建时间 |

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=ListDashboardAccessLinks&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "Page": 1,
    "Size": 20,
    "AgentId": "ar-xxxxxxxx",
    "LinkType": "share",
    "Status": "active"
  }'
```

## 3.3 撤销短链接（DeleteDashboardAccessLink）

### 请求体

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `LinkId` | 是 | 短链接 ID |

### 响应体（SDK 侧常见字段）

| 字段（snake_case） | 说明 |
| :--- | :--- |
| `deleted` | 是否删除成功（CLI 会优先判断该字段） |

说明：若只返回 `Code=0` 但无 `deleted=true`，CLI 会给出“请检查服务端日志”的提示。

### cURL 示例

```bash
curl -X POST "${BASE_URL}/?Action=DeleteDashboardAccessLink&Version=${VERSION}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Ksc-Region: ${REGION}" \
  -H "Authorization: ${AUTH}" \
  -d '{
    "LinkId": "lnk-xxxxxxxx"
  }'
```

## 4. SDK 入参/出参对照（给接入方）

如直接用 Python SDK（`ksadk.api.client.AgentEngineClient`）：

- 入参通常是 snake_case（例如 `framework`, `artifact_type`, `env_vars`），SDK 内部会转换为网关 PascalCase。
- 出参默认是 snake_case 且通常为 `Data` 子对象。
- 关键方法：
  - `create_agent(data)` -> `CreateAgentProduct`
  - `get_agent(agent_id|name, include_api_key=False)` -> `GetAgent`
  - `update_agent(agent_id, data)` -> `UpdateAgent`
  - `list_agents(region)` -> `ListAgents`
  - `delete_agent(agent_id)` -> `DeleteAgent`
  - `create_dashboard_access_link(...)`
  - `list_dashboard_access_links(...)`
  - `delete_dashboard_access_link(...)`
