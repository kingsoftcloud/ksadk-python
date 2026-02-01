# Multi-Agent 旅行规划系统

基于 LangGraph 的多智能体协作示例，适配金山云模型。

## 📖 功能特性

### 1. 多智能体协作架构
- **主智能体（调度中心）**：识别意图、分派任务、兜底回答
- **航班助手**：专注航班查询和改签
- **酒店助手**：专注酒店查询和取消

### 2. 敏感操作权限控制
- **安全工具**（自动执行）：查询航班、查询酒店
- **敏感工具**（需要授权）：改签机票、取消酒店

### 3. 智能体状态栈管理
- 支持智能体间的委派和返回
- 保持完整的上下文不丢失
- 类似浏览器历史栈的工作方式

## 🚀 快速开始

### 1. 配置环境变量

```bash
cd examples/multi_agent_travel
cp .env.example .env
```

编辑 `.env` 文件，填写你的金山云 API Key：

```bash
OPENAI_API_BASE=http://kspmas.ksyun.com/v1
OPENAI_API_KEY=your-actual-api-key
MODEL_NAME=deepseek-v3.2
```

### 2. 运行方式

#### 方式一：使用 KsADK CLI（推荐）

```bash
# 交互式命令行
ksadk run .

# Web UI
ksadk web .
```

#### 方式二：直接运行 Python

```bash
python -m multi_agent.agent
```

## 💬 使用示例

### 示例对话 1：航班查询

```
你: 帮我查一下长沙到北京，2026-02-10 的航班

助手: [委派到航班助手]
      找到以下航班：
      1. MU5201  08:30-11:20  ¥1580
      2. CZ3157  14:15-17:05  ¥1680
      3. HU7604  19:45-22:35  ¥1520

你: 我要订第一个
```

### 示例对话 2：改签机票（敏感操作）

```
你: 帮我把机票改签到 CZ3157

助手: [触发权限确认]
      准备执行改签操作：
      - 订单号: TK123456
      - 新航班: CZ3157
      - 改签费: ¥200

      [系统会自动请求确认后执行]
```

### 示例对话 3：酒店查询 + 取消

```
你: 帮我查北京的酒店，2月10日入住，2月12日退房

助手: [委派到酒店助手]
      找到以下酒店：
      1. 北京国贸大酒店  ¥1280/晚  可退
      2. 北京希尔顿酒店  ¥980/晚   不可退
      3. 北京如家快捷    ¥320/晚   可退

你: 我要取消之前的订单 H10086

助手: [触发权限确认]
      准备取消酒店订单 H10086
      预计退款: ¥1200
```

## 🏗️ 架构说明

### 状态管理（State）

```python
{
    "messages": [...],           # 对话历史
    "user_info": {...},          # 用户信息
    "agent_stack": ["main"],     # 智能体调用栈
    "pending_action": None       # 待授权操作
}
```

### 智能体流转示例

```
用户: "帮我改签航班，然后取消酒店"

[main] 识别意图 → 先处理航班
  ↓
[entry_flights] 生成上下文
  ↓
[flights] 航班助手处理
  - 查询航班
  - 触发改签 → [approval] 权限确认
  - CompleteOrEscalate
  ↓
[main] 返回主助手 → 识别剩余任务
  ↓
[entry_hotels] 生成上下文
  ↓
[hotels] 酒店助手处理
  - 触发取消 → [approval] 权限确认
  - CompleteOrEscalate
  ↓
[main] 任务完成
```

## 🔧 扩展开发

### 添加新的智能体（如租车助手）

1. **定义工具**

```python
@tool
def search_cars(city: str, pickup_date: str) -> Dict[str, Any]:
    """查询租车"""
    pass

@tool
def book_car(car_id: str) -> Dict[str, Any]:
    """预订租车（敏感）"""
    pass
```

2. **添加路由工具**

```python
@tool
def ToCarAssistant() -> str:
    """委派到租车助手"""
    return "route:cars"
```

3. **创建助手节点**

```python
def car_assistant(state: TravelState) -> TravelState:
    llm = make_llm().bind_tools([search_cars, book_car, CompleteOrEscalate])
    # ... 实现逻辑
```

4. **在图中注册**

```python
workflow.add_node("cars", car_assistant)
workflow.add_node("entry_cars", create_entry_node("cars"))
# ... 添加边和路由
```

## 📚 技术细节

### 为什么用 agent_stack？

传统单一智能体：上下文容易混乱，所有逻辑耦合在一起

Multi-Agent + Stack：
- 每个助手只关注自己的领域
- 可以嵌套调用（航班助手调用酒店助手查冲突）
- 任何时候都能返回上一级

### 权限中断机制

```python
# 子助手检测到敏感工具调用
if tool_name in SENSITIVE_TOOLS:
    state["pending_action"] = {
        "tool": tool_name,
        "args": args,
        "tool_call_id": tool_call_id
    }
    # 路由到 approval 节点

# approval 节点处理
def handle_approval(state):
    # 真实场景：通过 Web UI 请求确认
    # 简化实现：直接执行并记录日志
    result = execute_tool(pending["tool"], pending["args"])
    return result
```

## 🎯 与原文章代码的差异

1. **LLM 配置**：适配金山云 OpenAI 兼容接口
2. **中断机制**：简化了 `interrupt()` 的实现，更适合 KsADK 运行
3. **项目结构**：符合 KsADK 的 `<project>/multi_agent/agent.py` 规范
4. **类型定义**：使用 `TravelState(dict)` 替代 `TypedDict`，兼容性更好
5. **持久化**：添加了 `MemorySaver` 支持会话保持

## 🐛 常见问题

### Q1: 运行报错 "No API key provided"

**A:** 检查 `.env` 文件是否配置正确，确保 `OPENAI_API_KEY` 已设置

### Q2: 模型返回格式不符合预期

**A:** 尝试切换模型：
```bash
# .env 中修改
MODEL_NAME=glm-4  # 或其他支持的模型
```

### Q3: 敏感操作没有触发确认

**A:** 当前是简化实现，直接执行。如需真实确认流程，可集成：
- `ksadk web` 的 Web UI 确认弹窗
- 交互式命令行的 `input()` 确认

### Q4: 如何查看完整的执行流程？

**A:** 在 `agent.py` 中添加日志：

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## 📖 参考资料

- [LangGraph 官方文档](https://langchain-ai.github.io/langgraph/)
- [KsADK 开发指南](../../docs/KsADK_Implementation_Guide.md)
- [多智能体架构设计](../../docs/KsADK_MultiFramework_Observability_Guide.md)

## 📄 License

MIT License - 与 KsADK 主项目保持一致
