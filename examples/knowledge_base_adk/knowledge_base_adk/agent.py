"""知识库问答助手 - ADK 示例

演示如何在 ADK Agent 中显式集成金山云知识库能力。
通过 import 知识库工具并添加到 tools 列表中使用。

使用方式:
    1. 复制 .env.example 为 .env 并填入配置
    2. 运行: agentengine run .
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(Path(__file__).parent.parent / ".env")

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

# ============== 知识库工具 ==============
# 显式导入知识库检索工具，自动从环境变量读取配置
from ksadk.knowledge_base.adk_tool import search_knowledge_base

# ============== 模型配置 ==============
model = LiteLlm(
    model=f"openai/{os.getenv('MODEL_NAME', 'deepseek-v3.2')}",
    api_base=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
)


# ============== 自定义工具 ==============
def summarize_results(text: str) -> dict:
    """对检索结果进行摘要整理

    当知识库返回的内容较多时，可以使用此工具进行归纳总结。

    Args:
        text: 需要摘要的文本内容

    Returns:
        包含摘要结果的字典
    """
    summary = text[:500] + "..." if len(text) > 500 else text
    return {"status": "success", "summary": summary}


# ============== Agent 定义 ==============
root_agent = Agent(
    name="knowledge_base_assistant",
    model=model,
    description="基于知识库的智能问答助手，能够从企业知识库中检索信息并回答用户问题。",
    instruction="""你是一个专业的知识库问答助手。你的主要职责是根据用户的问题，
从知识库中检索相关信息，并给出准确、有帮助的回答。

## 工作流程

1. **理解问题**: 仔细分析用户的问题，提取关键信息
2. **检索知识**: 使用 search_knowledge_base 工具从知识库中检索相关内容
3. **整合回答**: 基于检索到的内容，组织清晰、准确的回答

## 使用工具

- **search_knowledge_base**: 从知识库中检索与查询相关的文档片段。
  传入用户问题的关键词或完整问题作为 query 参数。
- **summarize_results**: 当检索结果过长时，可以用此工具做摘要。

## 回答原则

- 优先使用知识库中的内容来回答问题
- 如果知识库中没有找到相关信息，如实告知用户
- 在回答中标注信息来源（如文档名称）
- 回答要简洁明了，条理清晰
- 如果用户的问题不够明确，可以请用户补充细节
""",
    tools=[
        search_knowledge_base,  # 知识库检索工具
        summarize_results,
    ],
)
