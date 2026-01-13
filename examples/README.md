# 智能助手 Demo

两个功能完全相同的智能助手 Agent，分别使用 Google ADK 和 LangGraph 框架实现。

## 功能

支持 **9 种工具**：

| 工具 | 功能 |
|------|------|
| `get_weather` | 查询城市天气 |
| `calculate` | 数学计算器 |
| `search_web` | 网络搜索（模拟） |
| `get_current_time` | 获取当前时间 |
| `create_note` | 创建笔记 |
| `list_notes` | 列出笔记 |
| `create_task` | 创建任务 |
| `list_tasks` | 列出任务 |
| `complete_task` | 完成任务 |

## 运行

### 1. 配置环境

```bash
# ADK 版本
cd smart_assistant_adk
cp .env.example .env
# 编辑 .env 填入模型配置

# LangGraph 版本  
cd smart_assistant_langgraph
cp .env.example .env
# 编辑 .env 填入模型配置
```

### 2. 启动 Agent

```bash
# 使用 ksadk 统一启动
cd smart_assistant_adk  # 或 smart_assistant_langgraph
agentengin run .
```

## 示例对话

```
用户: 今天北京天气怎么样？

助手: 北京今天天气晴朗，气温 25°C，湿度 45%，东北风3级。
      是个适合外出的好天气！

用户: 帮我计算一下 (100 + 200) * 0.15

助手: 计算结果：(100 + 200) * 0.15 = 45.0

用户: 创建一个高优先级任务：准备周五的演示

助手: ✅ 任务创建成功！
      - 任务ID: 1
      - 标题: 准备周五的演示
      - 优先级: 高
      - 状态: 待完成
```
