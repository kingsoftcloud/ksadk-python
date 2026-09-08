# KsADK Runner 层统一确认机制设计

## 1. 设计目标

为 KsADK 所有 Runner（LangGraph/LangChain/ADK）提供统一的：
- ✅ 敏感操作确认机制
- ✅ 中断/恢复能力
- ✅ 会话持久化
- ✅ 与框架无关的抽象层

## 2. 核心接口设计

### 2.1 确认协议（ApprovalProtocol）

```python
# ksadk/runners/protocols/approval.py

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Callable, Awaitable


class ActionType(Enum):
    """操作类型"""
    TOOL_CALL = "tool_call"           # 工具调用
    DATA_MODIFICATION = "data_mod"    # 数据修改
    EXTERNAL_API = "external_api"     # 外部 API 调用
    COST_INCURRING = "cost_incur"     # 产生费用的操作


class ApprovalDecision(Enum):
    """确认决策"""
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"  # 延迟决策


@dataclass
class ApprovalRequest:
    """确认请求"""
    action_id: str                    # 操作唯一 ID
    action_type: ActionType           # 操作类型
    tool_name: Optional[str] = None   # 工具名称
    args: Dict[str, Any] = None       # 工具参数
    description: str = ""             # 可读描述
    metadata: Dict[str, Any] = None   # 额外元数据

    def to_prompt(self) -> str:
        """生成用户友好的提示文本"""
        if self.tool_name:
            args_str = ", ".join(f"{k}={v}" for k, v in (self.args or {}).items())
            return (
                f"⚠️  需要您确认敏感操作\n\n"
                f"操作类型：{self.tool_name}\n"
                f"参数：{args_str}\n"
                f"描述：{self.description}\n\n"
                f"请确认是否执行？(输入 '确认' 或 '取消')"
            )
        return self.description


@dataclass
class ApprovalResponse:
    """确认响应"""
    action_id: str
    decision: ApprovalDecision
    reason: Optional[str] = None
    metadata: Dict[str, Any] = None


class ApprovalHandler:
    """确认处理器（抽象基类）"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """请求用户确认（子类必须实现）"""
        raise NotImplementedError

    def should_request_approval(self, tool_name: str, args: Dict[str, Any]) -> bool:
        """判断是否需要确认（可配置）"""
        # 默认策略：检查工具是否在敏感列表中
        return tool_name in self.get_sensitive_tools()

    def get_sensitive_tools(self) -> set[str]:
        """获取敏感工具列表（可配置）"""
        return {
            "cancel_ticket", "update_ticket", "cancel_hotel",
            "delete_data", "send_email", "charge_payment"
        }


class InteractiveApprovalHandler(ApprovalHandler):
    """交互式确认处理器（命令行）"""

    def __init__(self, input_callback: Callable[[str], Awaitable[str]]):
        """
        Args:
            input_callback: 异步输入回调函数（由 BaseRunner 注入）
        """
        self.input_callback = input_callback

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """通过命令行交互请求确认"""
        prompt = request.to_prompt()

        # 调用外部输入回调（由 Runner 提供）
        user_input = await self.input_callback(prompt)

        # 解析用户输入
        normalized = user_input.strip().lower()

        if normalized in ["确认", "yes", "y", "是", "好的", "可以", "approve"]:
            decision = ApprovalDecision.APPROVED
        elif normalized in ["取消", "no", "n", "否", "不要", "算了", "reject"]:
            decision = ApprovalDecision.REJECTED
        else:
            decision = ApprovalDecision.DEFERRED

        return ApprovalResponse(
            action_id=request.action_id,
            decision=decision,
            reason=user_input
        )


class AutoApprovalHandler(ApprovalHandler):
    """自动确认处理器（用于测试/非交互场景）"""

    def __init__(self, auto_approve: bool = False):
        self.auto_approve = auto_approve

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        decision = ApprovalDecision.APPROVED if self.auto_approve else ApprovalDecision.REJECTED
        return ApprovalResponse(
            action_id=request.action_id,
            decision=decision,
            reason="auto"
        )
```

### 2.2 中断管理器（InterruptManager）

```python
# ksadk/runners/protocols/interrupt.py

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from datetime import datetime
import uuid


@dataclass
class InterruptPoint:
    """中断点"""
    interrupt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    state: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    approval_request: Optional['ApprovalRequest'] = None


class InterruptManager:
    """中断管理器（支持暂停/恢复）"""

    def __init__(self):
        self._interrupts: Dict[str, InterruptPoint] = {}

    def create_interrupt(
        self,
        state: Dict[str, Any],
        reason: str = "",
        approval_request: Optional['ApprovalRequest'] = None
    ) -> InterruptPoint:
        """创建中断点"""
        interrupt = InterruptPoint(
            state=state,
            reason=reason,
            approval_request=approval_request
        )
        self._interrupts[interrupt.interrupt_id] = interrupt
        return interrupt

    def get_interrupt(self, interrupt_id: str) -> Optional[InterruptPoint]:
        """获取中断点"""
        return self._interrupts.get(interrupt_id)

    def resolve_interrupt(self, interrupt_id: str, response: 'ApprovalResponse'):
        """解决中断点（用户确认后）"""
        interrupt = self._interrupts.pop(interrupt_id, None)
        return interrupt

    def has_pending_interrupts(self) -> bool:
        """是否有待处理的中断"""
        return len(self._interrupts) > 0
```

### 2.3 会话持久化（SessionStore）

```python
# ksadk/runners/protocols/session.py

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import json
import os
from pathlib import Path


class SessionStore(ABC):
    """会话存储（抽象基类）"""

    @abstractmethod
    async def save(self, session_id: str, state: Dict[str, Any]) -> None:
        """保存会话状态"""
        pass

    @abstractmethod
    async def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        """加载会话状态"""
        pass

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """删除会话"""
        pass


class InMemorySessionStore(SessionStore):
    """内存会话存储（用于开发/测试）"""

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}

    async def save(self, session_id: str, state: Dict[str, Any]) -> None:
        self._sessions[session_id] = state

    async def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class FileSessionStore(SessionStore):
    """文件会话存储（本地持久化）"""

    def __init__(self, storage_dir: str = ".ksadk/sessions"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.json"

    async def save(self, session_id: str, state: Dict[str, Any]) -> None:
        path = self._get_session_path(session_id)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    async def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._get_session_path(session_id)
        if not path.exists():
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    async def delete(self, session_id: str) -> None:
        path = self._get_session_path(session_id)
        if path.exists():
            path.unlink()
```

## 3. BaseRunner 集成

### 3.1 修改 BaseRunner

```python
# ksadk/runners/base_runner.py

class BaseRunner:
    """基础 Runner（所有框架 Runner 的基类）"""

    def __init__(
        self,
        project_dir: str,
        approval_handler: Optional[ApprovalHandler] = None,
        session_store: Optional[SessionStore] = None,
    ):
        self.project_dir = project_dir

        # 确认处理器（默认交互式）
        self.approval_handler = approval_handler or InteractiveApprovalHandler(
            input_callback=self._get_user_input
        )

        # 会话存储（默认内存）
        self.session_store = session_store or InMemorySessionStore()

        # 中断管理器
        self.interrupt_manager = InterruptManager()

    async def _get_user_input(self, prompt: str) -> str:
        """获取用户输入（供 ApprovalHandler 调用）"""
        import questionary

        # 暂停当前流式输出（如果有）
        self._pause_streaming()

        # 显示提示并等待输入
        print(f"\n{prompt}\n")
        user_input = await questionary.text("👉 您的决定:").ask_async()

        # 恢复流式输出
        self._resume_streaming()

        return user_input or ""

    def _pause_streaming(self):
        """暂停流式输出（子类实现）"""
        pass

    def _resume_streaming(self):
        """恢复流式输出（子类实现）"""
        pass

    async def _handle_tool_call(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """处理工具调用（带确认机制）"""

        # 检查是否需要确认
        if self.approval_handler.should_request_approval(tool_name, args):
            # 创建确认请求
            request = ApprovalRequest(
                action_id=str(uuid.uuid4()),
                action_type=ActionType.TOOL_CALL,
                tool_name=tool_name,
                args=args,
                description=f"将要执行工具：{tool_name}"
            )

            # 请求用户确认
            response = await self.approval_handler.request_approval(request)

            # 根据用户决策执行
            if response.decision == ApprovalDecision.APPROVED:
                return await self._execute_tool(tool_name, args)
            elif response.decision == ApprovalDecision.REJECTED:
                return {"error": "用户取消操作", "tool": tool_name}
            else:
                # 延迟决策：创建中断点
                interrupt = self.interrupt_manager.create_interrupt(
                    state={"tool_name": tool_name, "args": args},
                    approval_request=request
                )
                return {"interrupted": True, "interrupt_id": interrupt.interrupt_id}

        # 不需要确认，直接执行
        return await self._execute_tool(tool_name, args)

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """实际执行工具（子类实现）"""
        raise NotImplementedError
```

### 3.2 LangGraphRunner 适配

```python
# ksadk/runners/langgraph_runner.py

class LangGraphRunner(BaseRunner):
    """LangGraph Runner（支持统一确认机制）"""

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式执行（带确认支持）"""

        # 检查是否有待恢复的中断
        session_id = input_data.get("session_id")
        if session_id:
            saved_state = await self.session_store.load(session_id)
            if saved_state and self.interrupt_manager.has_pending_interrupts():
                # 恢复中断点
                yield {"type": "info", "message": "检测到未完成的操作，继续执行..."}

        # 正常流式输出
        async for event in self._agent.astream_events(input_data, version="v2"):
            event_type = event["event"]

            # 拦截工具调用事件
            if event_type == "on_tool_start":
                tool_name = event["name"]
                tool_input = event["data"].get("input", {})

                # 通过统一的确认机制处理
                result = await self._handle_tool_call(tool_name, tool_input)

                # 如果被中断，保存状态并等待确认
                if isinstance(result, dict) and result.get("interrupted"):
                    yield {
                        "type": "interrupt",
                        "interrupt_id": result["interrupt_id"],
                        "message": "操作已暂停，等待您的确认"
                    }

                    # 保存会话状态
                    await self.session_store.save(session_id, {
                        "messages": input_data.get("messages", []),
                        "interrupt_id": result["interrupt_id"]
                    })
                    return

                # 如果被拒绝，返回错误
                if isinstance(result, dict) and "error" in result:
                    yield {"type": "error", "message": result["error"]}
                    continue

                # 正常执行，继续流式输出
                yield {
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": result
                }

            # 其他事件正常处理
            else:
                yield self._process_event(event)
```

## 4. 配置化的敏感工具策略

```python
# ksadk/configs/approval_config.py

from dataclasses import dataclass
from typing import Set, Dict, Any


@dataclass
class ApprovalConfig:
    """确认配置"""

    # 敏感工具列表（需要确认）
    sensitive_tools: Set[str] = None

    # 自动确认模式（测试用）
    auto_approve: bool = False

    # 确认超时时间（秒）
    timeout: int = 300

    # 自定义确认逻辑
    custom_check: callable = None

    def __post_init__(self):
        if self.sensitive_tools is None:
            self.sensitive_tools = {
                "cancel_ticket", "update_ticket", "cancel_hotel",
                "delete_data", "send_email", "charge_payment"
            }

    def should_approve(self, tool_name: str, args: Dict[str, Any]) -> bool:
        """判断是否需要确认"""
        if self.custom_check:
            return self.custom_check(tool_name, args)
        return tool_name in self.sensitive_tools


# 在 ksadk.yaml 中配置
"""
approval:
  sensitive_tools:
    - cancel_ticket
    - update_ticket
    - delete_data
  auto_approve: false  # 生产环境必须为 false
  timeout: 300
"""
```

## 5. 使用示例

### 5.1 在项目中配置

```yaml
# ksadk.yaml
framework: langgraph
approval:
  enabled: true
  sensitive_tools:
    - cancel_ticket
    - update_ticket
    - cancel_hotel
  handler: interactive  # interactive | auto | custom
```

### 5.2 在代码中使用

```python
# 方式 1: 使用默认配置
runner = LangGraphRunner(project_dir=".")

# 方式 2: 自定义确认处理器
custom_handler = InteractiveApprovalHandler(
    input_callback=my_custom_input_function
)
runner = LangGraphRunner(
    project_dir=".",
    approval_handler=custom_handler
)

# 方式 3: 测试模式（自动确认）
runner = LangGraphRunner(
    project_dir=".",
    approval_handler=AutoApprovalHandler(auto_approve=True)
)
```

## 6. 实现优先级

### Phase 1: 核心协议（1-2 周）
- [x] `ApprovalProtocol` 接口定义
- [x] `InteractiveApprovalHandler` 实现
- [x] `BaseRunner._handle_tool_call()` 集成

### Phase 2: Runner 适配（2-3 周）
- [ ] `LangGraphRunner` 适配（支持中断/恢复）
- [ ] `LangChainRunner` 适配
- [ ] `ADKRunner` 适配

### Phase 3: 持久化 & 高级特性（2-3 周）
- [ ] `SessionStore` 实现（File/Redis）
- [ ] `InterruptManager` 完整实现
- [ ] Web UI 确认支持（ksadk web 模式）

### Phase 4: 生产优化（1-2 周）
- [ ] 性能优化
- [ ] 错误处理
- [ ] 文档完善
- [ ] 单元测试

## 7. 优势

### 7.1 框架无关
- ✅ 统一的确认协议，适用于所有框架
- ✅ 不依赖 LangGraph 的 `interrupt()`
- ✅ 不依赖 LangChain 的特定实现

### 7.2 灵活扩展
- ✅ 可配置的敏感工具列表
- ✅ 可插拔的确认处理器
- ✅ 支持自定义确认逻辑

### 7.3 生产就绪
- ✅ 支持持久化会话
- ✅ 支持中断/恢复
- ✅ 支持超时处理
- ✅ 完善的错误处理

### 7.4 用户体验
- ✅ 清晰的确认提示
- ✅ 统一的交互方式
- ✅ 支持批量确认（可选）

## 8. 与现有代码兼容

### 8.1 向后兼容
```python
# 旧代码（不使用确认机制）
runner = LangGraphRunner(project_dir=".")
result = await runner.invoke({"input": "..."})

# 新代码（启用确认机制）
runner = LangGraphRunner(
    project_dir=".",
    approval_handler=InteractiveApprovalHandler(...)
)
result = await runner.invoke({"input": "..."})
```

### 8.2 渐进式迁移
- Phase 1: 添加新接口，不影响现有代码
- Phase 2: 逐步迁移各 Runner
- Phase 3: 标记旧 API 为 deprecated
- Phase 4: 移除旧 API（主版本升级）

## 9. 参考实现

参考业界最佳实践：
- LangGraph 的 `interrupt()` 机制
- LangSmith 的审批流程
- AutoGen 的 human-in-the-loop
- Temporal 的工作流中断

---

## 总结

这套设计提供了：
1. ✅ **统一抽象**：所有框架共享同一套确认协议
2. ✅ **灵活配置**：支持交互式/自动/自定义处理器
3. ✅ **生产就绪**：持久化、中断恢复、错误处理
4. ✅ **向后兼容**：不影响现有代码

建议优先实现 **Phase 1（核心协议）** 和 **Phase 2（LangGraph 适配）**，这样可以快速验证架构可行性。
