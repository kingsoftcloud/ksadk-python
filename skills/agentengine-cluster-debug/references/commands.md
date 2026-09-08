# AgentEngine 集群排障命令参考

## 1. 管理集群

先切管理集群 kubeconfig：

```bash
export KUBECONFIG=~/.kube/agentengine-pre
```

或：

```bash
export KUBECONFIG=~/.kube/agentengine-online
```

常用管理侧入口：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine/agentengine-server
make status ENV=pre
make logs-tail ENV=pre
make debug ENV=pre
make shell ENV=pre
```

用途：

- 确认 control plane 是否健康
- 查 runtime 创建状态
- 从管理侧日志或现网脚本继续追 `compute cluster id`

## 2. 获取 compute cluster id 的落点

先记住这条规则：

- 不要先跑 `get-kubeconfig`，先拿 `compute cluster id`
- 不要编造数据库表名、字段名或内部接口

建议顺序：

1. 在管理集群按当前服务端查询入口或日志确认 runtime 当前状态
2. 按现网脚本、服务端日志或管理侧查询结果确认当前生效的 `compute cluster id`
3. 如需确认算力集群地址/配置，进入管理集群中的 `agent-runtime-service` 再按当前查询入口确认

相关代码入口：

- `/Users/xiayu/kingsoft/code/agent-sdk/agentengine/agent-runtime-service/api/service/agents/create_agent_runtime.go`
- `/Users/xiayu/kingsoft/code/agent-sdk/agentengine/agent-runtime-service/controller/agentctl/agent_runtime.go`

## 3. 获取算力 kubeconfig

仅在目标是 `kce1.0`，或你已经处在能访问目标集群的环境里时使用：

```bash
cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine/get-kubeconfig
go run . \
  --cluster-id <compute-cluster-id> \
  --region cn-beijing-6 \
  --kubeconfig ~/.kube/config-<compute-cluster-id> \
  --set-default=false
```

注意：

- `kce1.0`：通常可在本地直接连
- `kce2.0`：通常需进入管理集群内的 `agentengine-server` 或 `agent-runtime-service` pod 后再连

## 4. 找 runtime pod

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl get pods -n <runtime-namespace> -o wide
```

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl describe pod -n <runtime-namespace> <pod-name>
```

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl logs -n <runtime-namespace> <pod-name> -c agent-runtime --tail=200
```

如需前一次重启日志：

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl logs -n <runtime-namespace> <pod-name> -c agent-runtime --previous --tail=200
```

## 5. 查看生效环境变量

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl -n <runtime-namespace> exec <pod-name> -c agent-runtime -- \
sh -lc 'python - <<'"'"'PY'"'"'
import os

for key in sorted(os.environ):
    if key.startswith(("KSADK_", "KSYUN_", "KSC_")) or any(token in key for token in ("ACCESS_KEY", "SECRET_KEY", "AK", "SK")):
        value = os.environ.get(key, "")
        masked = "<set>" if value else "<empty>"
        if any(token in key for token in ("ACCESS_KEY", "SECRET_KEY", "AK", "SK")) and value:
            masked = f"{value[:2]}***{value[-2:]}" if len(value) >= 4 else "***"
        print(f"{key}={masked}")
PY'
```

## 6. 检查 Python 依赖

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl -n <runtime-namespace> exec <pod-name> -c agent-runtime -- \
sh -lc 'python - <<'"'"'PY'"'"'
import importlib

mods = [
    "ksyun.client.aicp",
    "kingsoftcloud_sdk_python",
]

for name in mods:
    try:
        importlib.import_module(name)
        print(name, "OK")
    except Exception as exc:
        print(name, "FAIL", repr(exc))
PY'
```

## 7. AICP DNS 与 TCP 探测

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl -n <runtime-namespace> exec <pod-name> -c agent-runtime -- \
sh -lc 'python - <<'"'"'PY'"'"'
import socket

host = "aicp.inner.api.ksyun.com"

try:
    print("dns:", socket.gethostbyname(host))
except Exception as exc:
    print("dns: FAIL", repr(exc))

for port in (80, 443):
    try:
        socket.create_connection((host, port), timeout=10).close()
        print(f"tcp:{port}: OK")
    except Exception as exc:
        print(f"tcp:{port}: FAIL {exc!r}")
PY'
```

这是已验证过的排查模式，不要求使用真实 namespace 或固定 pod 名。

## 8. 进入 pod 做最小脚本验证

```bash
KUBECONFIG=$HOME/.kube/config-<compute-cluster-id> \
kubectl -n <runtime-namespace> exec <pod-name> -c agent-runtime -- sh
```

然后在 pod 内：

- 优先运行项目或镜像里已经存在的检查脚本
- 如果没有，再写一次性 Python 片段

最小验证脚本至少打印：

- endpoint
- scheme
- region
- resource id
- 异常类型
- 异常消息

不要默认 `scripts/check_kb.py` 或 `scripts/check_ltm.py` 一定存在，先确认镜像内容。

## 9. 快速归类

- import 失败：镜像/依赖问题
- DNS 失败：pod DNS 或网络问题
- TCP 失败：egress、路由、ACL、安全组或基础设施问题
- HTTP/SDK 返回 `InnerAccountCanOnlyAccessThroughIntranet`：inner/public endpoint 不匹配，或必须走内网
- HTTP/SDK 返回 `Dataset not found` / `NotFound`：资源 ID、租户范围或环境错误
