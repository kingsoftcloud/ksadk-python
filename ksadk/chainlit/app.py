"""
Chainlit 适配器 - 为 LangChain/LangGraph Agent 提供 Web UI

支持：
- 流式输出
- 思考过程显示
- 工具调用显示
- Session 记忆（对话历史）
"""

import os
import sys
from pathlib import Path

try:
    import chainlit as cl
except ImportError:
    raise ImportError("请安装 chainlit: pip install chainlit")


def create_chainlit_app(project_dir: str):
    """创建 Chainlit 应用配置"""
    from ksadk.detection import FrameworkDetector
    from ksadk.runners import create_runner
    from ksadk.configs import setup_environment

    project_path = Path(project_dir).resolve()
    setup_environment(project_path)
    
    detector = FrameworkDetector(str(project_path))
    result = detector.detect()
    
    if result.type.value == "unknown":
        raise ValueError("未检测到支持的框架")
    
    runner = create_runner(result, str(project_path))
    runner.load_agent()
    
    return runner, result


_runner = None
_detection_result = None


def init_runner():
    """延迟初始化 Runner"""
    global _runner, _detection_result
    if _runner is None:
        project_dir = os.environ.get("KSADK_PROJECT_DIR", ".")
        _runner, _detection_result = create_chainlit_app(project_dir)
    return _runner, _detection_result


@cl.on_chat_start
async def on_chat_start():
    """聊天开始时初始化"""
    runner, result = init_runner()
    
    # 生成稳定的 session_id 用于 LangGraph checkpointer
    import uuid
    session_id = cl.user_session.get("id") or str(uuid.uuid4())
    
    cl.user_session.set("runner", runner)
    cl.user_session.set("session_id", session_id)
    cl.user_session.set("history", [])
    
    framework_name = result.type.value.upper()
    agent_name = result.name
    await cl.Message(
        content=f"🤖 **{agent_name}** ({framework_name}) 已就绪\n\n请输入您的问题...",
    ).send()
    
    # Debug: 打印 session_id
    print(f"[Chainlit] Session ID: {session_id}")


@cl.on_message
async def on_message(message: cl.Message):
    """处理用户消息"""
    runner = cl.user_session.get("runner")
    session_id = cl.user_session.get("session_id")
    history = cl.user_session.get("history", [])
    
    if not runner:
        await cl.Message(content="❌ Runner 未初始化").send()
        return
    
    # 创建响应消息 (延迟发送，确保 Thinking 在前)
    msg = None
    
    # 准备输入（包含历史记录）
    input_data = {
        "input": message.content,
        "session_id": session_id,
        "history": history.copy(),  # 复制一份避免被修改
    }
    
    # Debug: 打印 history 长度
    print(f"[Chainlit] History length: {len(history)}, Session: {session_id}")
    
    full_response = ""
    thinking_content = ""
    thinking_step = None
    active_tool_steps = {}  # type: dict[str, cl.Step]
    
    try:
        async for chunk in runner.stream(input_data):
            chunk_type = chunk.get("type", "")
            
            if chunk_type == "text":
                delta = chunk.get("delta", "")
                full_response += delta
                
                if msg is None:
                    msg = cl.Message(content="")
                    await msg.send()
                
                await msg.stream_token(delta)
                
            elif chunk_type == "thinking":
                # 如果已有正在发送的消息，先结束它，确保 Thinking 在新消息之前显示
                if msg:
                    await msg.update()
                    msg = None
                    full_response = ""
                
                delta = chunk.get("delta", "")
                thinking_content += delta
                
                if thinking_step is None:
                    thinking_step = cl.Step(name="🧠 Thinking", type="llm")
                    await thinking_step.__aenter__()
                
                await thinking_step.stream_token(delta)
                
            elif chunk_type == "tool_call":
                # 如果已有正在发送的消息，先结束它
                if msg:
                    await msg.update()
                    msg = None
                    full_response = ""
                
                if thinking_step:
                    thinking_step.output = thinking_content
                    await thinking_step.__aexit__(None, None, None)
                    thinking_step = None
                
                tool_name = chunk.get("tool_name", "unknown")
                tool_args = chunk.get("tool_args", {})
                run_id = chunk.get("run_id") or tool_name
                
                # 创建并未关闭 step
                step = cl.Step(name=f"🔧 {tool_name}", type="tool")
                step.input = tool_args
                await step.__aenter__()
                active_tool_steps[run_id] = step
            
            elif chunk_type == "tool_result":
                tool_name = chunk.get("tool_name", "unknown")
                tool_output = chunk.get("tool_output", "")
                run_id = chunk.get("run_id") or tool_name
                
                if run_id in active_tool_steps:
                    step = active_tool_steps.pop(run_id)
                    step.output = tool_output
                    await step.__aexit__(None, None, None)
                    
            elif chunk_type == "final":
                if not full_response:
                    full_response = chunk.get("output", "")
                    if msg is None:
                        msg = cl.Message(content="")
                        await msg.send()
                    await msg.stream_token(full_response)
        
        if thinking_step:
            thinking_step.output = thinking_content
            await thinking_step.__aexit__(None, None, None)
            
        # 关闭所有未关闭的工具 step
        for step in active_tool_steps.values():
            await step.__aexit__(None, None, None)
        
        if msg is None:
             # 如果没有任何输出，发送空消息或 final response (已处理)
             msg = cl.Message(content=full_response)
             await msg.send()
        else:
            msg.content = full_response
            await msg.update()
        
        # 更新对话历史
        history.append({"role": "user", "content": message.content})
        history.append({"role": "assistant", "content": full_response})
        cl.user_session.set("history", history)
        
    except Exception as e:
        if thinking_step:
            try:
                await thinking_step.__aexit__(None, None, None)
            except:
                pass
        await cl.Message(content=f"❌ 错误: {str(e)}").send()


@cl.on_stop
async def on_stop():
    """用户停止生成"""
    pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        os.environ["KSADK_PROJECT_DIR"] = sys.argv[1]
    print("请使用 chainlit run 命令启动")
