# Workspace Files PVC Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **历史计划说明：** 这份文档记录的是当时的实施路线，不再代表当前代码的最终真相。当前以 `docs/agentengine-runtime-common-设计方案.md` 与实际代码为准。特别是 OpenClaw 的 `WorkspaceFiles` 能力目前仍未在 runtime/gateway 主链路开启，不能把本文里的计划项当成已落地事实。

**Goal:** 在 `workspace-files-v1` 分支上定向移植 serverless PVC 存储能力，并与已完成的 workspace files v1 主链路集成，覆盖 `agentengine-server` 与 `ksadk-python` 两个仓库。

**Architecture:** 保持 workspace files 对外 contract 不变，只把 PVC 视为 agent state root 的持久化手段。Server 侧补 `Storage` schema、数据库 JSON 列、serverless client 透传与 `GetAgent` 回显；CLI/SDK 侧补统一 storage 参数和 framework 默认挂载目录；`GetAgentUiBootstrap` 对 `openclaw` 开启 `WorkspaceFiles` capability，但对外仍只暴露各 framework 的 `workspace` 子目录。

**Tech Stack:** Python, FastAPI, SQLAlchemy, Pydantic, Click, pytest, AgentEngine Server, ksadk CLI/SDK

---

### Task 1: 打通 Server 侧 Storage 数据模型与 serverless 透传

**Files:**
- Modify: `agentengine-server/app/models/agent.py`
- Modify: `agentengine-server/app/main.py`
- Modify: `agentengine-server/app/services/agent_service.py`
- Modify: `agentengine-server/app/services/serverless_api.py`
- Test: `agentengine-server/tests/test_chat_actions.py`

- [ ] **Step 1: 先写失败测试，锁定 storage 回显 contract**

```python
async def test_get_agent_returns_storage_config(client, db_session, seeded_agent):
    seeded_agent.storage_config = {"mount_path": "/home/node/.openclaw", "size_gi": 20}
    db_session.add(seeded_agent)
    await db_session.commit()

    response = await client.post(
        "/agentengine/api/v1/GetAgent",
        json={"AgentId": seeded_agent.id},
    )

    assert response.status_code == 200
    payload = response.json()["Data"]
    assert payload["Deployment"]["Storage"] == {
        "MountPath": "/home/node/.openclaw",
        "SizeGi": 20,
    }
```

- [ ] **Step 2: 运行单测，确认当前失败**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine-server && pytest tests/test_chat_actions.py -k storage -q`

Expected: FAIL，`Deployment.Storage` 缺失或 `storage_config` 字段不存在。

- [ ] **Step 3: 在 Agent 模型新增 JSON 列，并让 auto-migrate 自动补列**

```python
# agentengine-server/app/models/agent.py
storage_config: Mapped[Optional[dict]] = mapped_column(JSON)
```

```python
# agentengine-server/app/main.py
# 不新增专门 migration 文件，复用现有 Base.metadata.create_all + auto_migrate_columns 机制
```

- [ ] **Step 4: 给 serverless API client 增加 StorageConfiguration，并补 from_dict/to_dict**

```python
@dataclass
class StorageConfiguration:
    mount_path: Optional[str] = None
    size_gi: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if self.mount_path:
            result["mountPath"] = self.mount_path
        if self.size_gi is not None:
            result["sizeGi"] = self.size_gi
        return result
```

- [ ] **Step 5: 在 AgentService 中统一存储配置的入库、下发与回显**

```python
def _storage_cfg_to_dict(storage_cfg: Any) -> Dict[str, Any]:
    ...
    mount_path = raw.get("mount_path", raw.get("mountPath", raw.get("MountPath")))
    size_gi = raw.get("size_gi", raw.get("sizeGi", raw.get("SizeGi")))
    return {
        "mount_path": str(mount_path).strip() if mount_path else None,
        "size_gi": int(size_gi) if size_gi is not None else None,
    }
```

- [ ] **Step 6: 再跑同一组测试，确认变绿**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine-server && pytest tests/test_chat_actions.py -k storage -q`

Expected: PASS。


### Task 2: 补 Server Action schema，并把 OpenClaw 纳入 WorkspaceFiles capability

**Files:**
- Modify: `agentengine-server/app/api/v1/actions/agent_actions.py`
- Modify: `agentengine-server/app/api/v1/actions/chat_actions.py`
- Test: `agentengine-server/tests/test_chat_actions.py`

- [ ] **Step 1: 先写失败测试，锁定 Create/GetAgent 的 Storage 与 openclaw capability**

```python
async def test_get_agent_ui_bootstrap_enables_workspace_files_for_openclaw(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.actions.chat_actions._WORKSPACE_SUPPORTED_FRAMEWORKS",
        {"adk", "langchain", "langgraph", "deepagents", "hermes", "openclaw"},
    )
    ...
    assert payload["Capabilities"]["WorkspaceFiles"] is True
    assert payload["WorkspaceFiles"]["Enabled"] is True
```

```python
def test_create_agent_schema_accepts_storage():
    request = CreateAgentSchema(
        Name="demo",
        Framework="openclaw",
        DeploymentType="Container",
        Storage={"MountPath": "/home/node/.openclaw", "SizeGi": 20},
    )
    assert request.Storage.SizeGi == 20
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine-server && pytest tests/test_chat_actions.py -k 'workspace or storage' -q`

Expected: FAIL，`openclaw` 不在 capability 白名单，schema 不接受 `Storage`。

- [ ] **Step 3: 增加 `StorageConfigSchema`，打通 CreateAgent/UpdateAgent 入参**

```python
class StorageConfigSchema(BaseModel):
    MountPath: Optional[str] = Field(None, description="PVC 挂载目录")
    SizeGi: Optional[int] = Field(None, ge=20, le=500, description="PVC 大小，单位 Gi")
```

- [ ] **Step 4: 在 bootstrap capability 中把 `openclaw` 纳入白名单**

```python
_WORKSPACE_SUPPORTED_FRAMEWORKS = {
    "adk",
    "langchain",
    "langgraph",
    "deepagents",
    "hermes",
    "openclaw",
}
```

- [ ] **Step 5: 重跑测试，确认 schema 与 capability 同时生效**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine-server && pytest tests/test_chat_actions.py -k 'workspace or storage' -q`

Expected: PASS。


### Task 3: 在 ksadk API client 与 serverless deploy provider 注入统一 Storage 参数

**Files:**
- Modify: `ksadk-python/ksadk/api/client.py`
- Modify: `ksadk-python/ksadk/deployment/base.py`
- Modify: `ksadk-python/ksadk/deployment/providers/serverless.py`
- Test: `ksadk-python/tests/test_client_workspace_files.py`
- Create: `ksadk-python/tests/test_client_storage_config.py`

- [ ] **Step 1: 先写失败测试，锁定 create_agent/update_agent 的 Storage payload**

```python
def test_create_agent_includes_storage_payload(fake_client):
    fake_client.create_agent(
        {
            "name": "demo",
            "framework": "adk",
            "artifact_type": "Code",
            "storage": {"mount_path": "/home/node/.agentengine", "size_gi": 20},
        }
    )
    request = fake_client.calls[-1]
    assert request["action"] == "CreateAgentProduct"
    assert request["params"]["Storage"] == {
        "MountPath": "/home/node/.agentengine",
        "SizeGi": 20,
    }
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_client_storage_config.py -q`

Expected: FAIL，`Storage` 未被下发。

- [ ] **Step 3: 给 deploy base 增加 storage 规格，并在 serverless provider 透传**

```python
class StorageSpec(BaseModel):
    mount_path: str = ""
    size_gi: int = 20

class DeployTarget(BaseModel):
    ...
    storage: StorageSpec = Field(default_factory=StorageSpec)
```

```python
request_data["storage"] = {
    "mount_path": target.storage.mount_path,
    "size_gi": target.storage.size_gi,
}
```

- [ ] **Step 4: 在 API client 中补 `Storage` 规范化**

```python
def _normalize_storage_payload(storage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(storage, dict):
        return None
    mount_path = str(storage.get("mount_path") or storage.get("mountPath") or "").strip()
    size_gi = storage.get("size_gi", storage.get("sizeGi"))
    if not mount_path and size_gi is None:
        return None
    payload: Dict[str, Any] = {}
    if mount_path:
        payload["MountPath"] = mount_path
    if size_gi is not None:
        payload["SizeGi"] = int(size_gi)
    return payload
```

- [ ] **Step 5: 重跑 storage client/provider 测试**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_client_storage_config.py -q`

Expected: PASS。


### Task 4: 在 ksadk CLI 为 serverless 默认开启 PVC，并支持用户自定义

**Files:**
- Modify: `ksadk-python/ksadk/cli/cmd_deploy.py`
- Modify: `ksadk-python/ksadk/cli/cmd_hermes.py`
- Modify: `ksadk-python/ksadk/cli/cmd_openclaw.py`
- Modify: `ksadk-python/ksadk/cli/global_options.py`
- Test: `ksadk-python/tests/test_cmd_hermes.py`
- Test: `ksadk-python/tests/test_cmd_openclaw.py`
- Create: `ksadk-python/tests/test_storage_defaults.py`

- [ ] **Step 1: 先写失败测试，锁定默认值、上下限和自定义行为**

```python
def test_openclaw_deploy_defaults_storage_to_state_root():
    payload = build_openclaw_request(...)
    assert payload["storage"] == {
        "mount_path": "/home/node/.openclaw",
        "size_gi": 20,
    }
```

```python
def test_storage_size_gi_must_be_between_20_and_500():
    with pytest.raises(click.BadParameter):
        validate_storage_size_gi(10)
    with pytest.raises(click.BadParameter):
        validate_storage_size_gi(600)
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_storage_defaults.py -q`

Expected: FAIL。

- [ ] **Step 3: 抽一个 CLI 公共 storage helper，统一默认挂载口径**

```python
DEFAULT_STORAGE_SIZE_GI = 20
MIN_STORAGE_SIZE_GI = 20
MAX_STORAGE_SIZE_GI = 500

def resolve_default_storage_mount_path(framework: str) -> str:
    mapping = {
        "adk": "/home/node/.agentengine",
        "langchain": "/home/node/.agentengine",
        "langgraph": "/home/node/.agentengine",
        "deepagents": "/home/node/.agentengine",
        "hermes": "/home/node/.hermes",
        "openclaw": "/home/node/.openclaw",
    }
    return mapping[framework]
```

- [ ] **Step 4: 在 CLI 暴露统一参数，并只对 serverless 默认自动开启**

```python
@click.option("--storage-size-gi", type=int, default=20, show_default=True)
@click.option("--storage-mount-path", default=None)
@click.option("--no-storage", is_flag=True, default=False)
```

```python
if target == "serverless" and not no_storage:
    deploy_target.storage.mount_path = storage_mount_path or resolve_default_storage_mount_path(framework)
    deploy_target.storage.size_gi = validate_storage_size_gi(storage_size_gi or 20)
```

- [ ] **Step 5: 对自定义挂载路径做最小校验**

```python
def validate_storage_mount_path(value: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or path == "/":
        raise click.BadParameter("存储挂载目录必须是绝对路径，且不能为 /")
    return path
```

- [ ] **Step 6: 重跑 CLI 测试**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_storage_defaults.py tests/test_cmd_hermes.py tests/test_cmd_openclaw.py -q`

Expected: PASS。


### Task 5: 让 code runtime 在自定义 mount path 下自动映射到 `AGENTENGINE_UI_DIR`

**Files:**
- Modify: `ksadk-python/ksadk/server/app.py`
- Modify: `ksadk-python/ksadk/sessions/local_service.py`
- Modify: `ksadk-python/tests/test_server_session_app.py`

- [ ] **Step 1: 先写失败测试，锁定自定义 state root -> workspace root 推导**

```python
async def test_workspace_files_runtime_uses_custom_agentengine_state_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", "/custom/state/ui")
    ...
    assert payload["workspace_path"] == "/custom/state/ui/workspace"
```

- [ ] **Step 2: 运行测试，确认当前失败或缺少推导逻辑**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_server_session_app.py -k workspace_files_runtime -q`

Expected: FAIL 或行为未覆盖。

- [ ] **Step 3: 保持 workspace files 只暴露 `workspace` 子目录，但把默认云端 state root 收敛到 `/home/node/.agentengine`**

```python
def _workspace_root_dir() -> Path:
    return resolve_local_session_dir() / "workspace"
```

```python
# 通过环境变量约定云端:
AGENTENGINE_UI_DIR=/home/node/.agentengine/ui
```

- [ ] **Step 4: 重跑 runtime 相关测试**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_server_session_app.py -k workspace_files_runtime -q`

Expected: PASS。


### Task 6: 更新文档，并补跨仓回归测试

**Files:**
- Modify: `ksadk-python/docs/workspace_files_v1_实施说明.md`
- Modify: `agentengine-server/tests/test_router_service_proxy.py`
- Modify: `ksadk-python/tests/test_openclaw_workspace_files_gating.py`
- Modify: `ksadk-python/tests/test_unified_agent_ui_local.py`

- [ ] **Step 1: 先写/更新失败测试，锁定文档对应的新口径**

```python
def test_openclaw_workspace_files_gating_enabled():
    assert "openclaw" in _WORKSPACE_SUPPORTED_FRAMEWORKS
```

```python
def test_bootstrap_workspace_fields_present_for_owner():
    assert payload["WorkspaceFiles"]["RootLabel"] == "workspace"
    assert payload["WorkspaceFiles"]["EntryAction"] == "ListWorkspaceFiles"
    assert payload["WorkspaceFiles"]["UploadAction"] == "AddWorkspaceFile"
    assert payload["WorkspaceFiles"]["ContentPath"] == "/agentengine/api/v1/GetWorkspaceFileContent"
```

- [ ] **Step 2: 运行相关测试，确认当前失败**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_openclaw_workspace_files_gating.py tests/test_unified_agent_ui_local.py -k workspace -q`

Expected: FAIL。

- [ ] **Step 3: 更新实施文档，明确最终口径**

```markdown
- KOP 已注册 `AddWorkspaceFile` / `ListWorkspaceFiles` / `DeleteWorkspaceFile` / `GetWorkspaceFileContent`
- OpenClaw 已纳入 `WorkspaceFiles` capability 白名单
- serverless PVC 默认开启
- `SizeGi` 约束：默认 20，最小 20，最大 500
- 默认挂载根：
  - code runtime: `/home/node/.agentengine`
  - Hermes: `/home/node/.hermes`
  - OpenClaw: `/home/node/.openclaw`
```

- [ ] **Step 4: 跑两仓最小回归集**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine-server && pytest tests/test_chat_actions.py tests/test_router_service_proxy.py -q`

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && pytest tests/test_client_storage_config.py tests/test_storage_defaults.py tests/test_server_session_app.py tests/test_openclaw_workspace_files_gating.py tests/test_unified_agent_ui_local.py -q`

Expected: 两边都 PASS。

- [ ] **Step 5: 跑更完整的验证命令**

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/agentengine-server && make test`

Run: `cd /Users/xiayu/kingsoft/code/agent-sdk/ksadk-python && make test`

Expected: 全量测试通过；如果失败，记录失败范围并在当前分支修复后重跑。
