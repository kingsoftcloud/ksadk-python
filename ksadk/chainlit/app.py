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
import json
import re
from pathlib import Path
from typing import Any

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


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _read_text_file(path: str, max_chars: int) -> tuple[str, bool]:
    """读取本地文本文件内容，返回 (text, truncated)。"""
    if not path:
        return "", False

    p = Path(path)
    if not p.exists() or not p.is_file() or max_chars <= 0:
        return "", False

    text_exts = {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".py",
        ".js",
        ".ts",
        ".sql",
        ".log",
    }

    if p.suffix.lower() not in text_exts:
        return "", False

    content = p.read_text(encoding="utf-8", errors="ignore")
    if len(content) > max_chars:
        return content[:max_chars], True
    return content, False


def _extract_upload_context(message: cl.Message) -> tuple[list[dict], str]:
    """提取上传文件元数据，并尽量附带可读文本内容。"""
    elements = getattr(message, "elements", None) or []
    if not elements:
        return [], ""

    files: list[dict] = []
    summary_lines: list[str] = []
    snippets: list[str] = []

    max_total_chars = 12000
    max_file_chars = 4000

    for idx, el in enumerate(elements, start=1):
        name = _get_field(el, "name") or f"file_{idx}"
        path = _get_field(el, "path")
        url = _get_field(el, "url")
        mime = _get_field(el, "mime")
        element_type = _get_field(el, "type")

        file_info: dict[str, Any] = {
            "name": name,
            "type": element_type,
            "mime": mime,
            "path": path,
            "url": url,
        }

        if path and Path(path).exists():
            try:
                file_info["size_bytes"] = Path(path).stat().st_size
            except OSError:
                pass

        extracted_text = ""
        truncated = False
        if path and max_total_chars > 0:
            extracted_text, truncated = _read_text_file(
                path=path,
                max_chars=min(max_file_chars, max_total_chars),
            )
            if extracted_text:
                max_total_chars -= len(extracted_text)
                file_info["text_content"] = extracted_text
                file_info["text_truncated"] = truncated

        files.append(file_info)

        size_label = ""
        if "size_bytes" in file_info:
            size_label = f", {file_info['size_bytes']} bytes"
        summary_lines.append(f"- {name} ({element_type or 'file'}{size_label})")

        if extracted_text:
            suffix = "\n[内容已截断]" if truncated else ""
            snippets.append(f"[{name}]\n{extracted_text}{suffix}")

    prompt_blocks = ["【用户上传文件】", *summary_lines]
    if snippets:
        prompt_blocks.append("\n【文件文本内容】")
        prompt_blocks.extend(snippets)
    else:
        prompt_blocks.append("\n【说明】当前上传文件未自动提取文本内容（如 PDF/音频/图片），请结合文件名或可访问 URL 使用。")

    return files, "\n".join(prompt_blocks)


def _extract_interrupt_message(interrupt_info: Any) -> str:
    if isinstance(interrupt_info, str):
        return interrupt_info

    if isinstance(interrupt_info, dict):
        for key in ("message", "prompt", "question", "content"):
            value = interrupt_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if "value" in interrupt_info:
            return _extract_interrupt_message(interrupt_info["value"])

    if isinstance(interrupt_info, (list, tuple)) and interrupt_info:
        first = interrupt_info[0]
        if hasattr(first, "value"):
            return _extract_interrupt_message(getattr(first, "value"))
        return _extract_interrupt_message(first)

    if hasattr(interrupt_info, "value"):
        return _extract_interrupt_message(getattr(interrupt_info, "value"))

    try:
        return json.dumps(interrupt_info, ensure_ascii=False, default=str)
    except Exception:
        return str(interrupt_info)


def _format_interrupt_prompt(interrupt_info: Any) -> str:
    message = _extract_interrupt_message(interrupt_info).strip()
    if not message:
        message = "Agent 需要人工确认后继续。"
    return (
        f"⏸️ 需要人工确认\n\n"
        f"{message}\n\n"
        "请回复 `yes` / `no`，或直接输入恢复参数（JSON/文本）。"
    )


def _parse_resume_input(text: str) -> Any:
    value = (text or "").strip()
    if not value:
        return True

    lower = value.lower()
    yes_words = {"y", "yes", "true", "ok", "approve", "同意", "确认", "通过"}
    no_words = {"n", "no", "false", "reject", "deny", "拒绝", "取消", "不通过"}

    if lower in yes_words:
        return True
    if lower in no_words:
        return False

    try:
        return json.loads(value)
    except Exception:
        return value


def _normalize_markdown(text: str) -> str:
    """轻量 markdown 规范化，降低渲染异常概率。"""
    if not text:
        return text

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # 若代码块围栏数量为奇数，自动补齐闭合围栏，避免后续内容被吞。
    if normalized.count("```") % 2 == 1:
        normalized = f"{normalized}\n```"

    def _normalize_prose_block(block: str) -> str:
        # 标题标记后补空格: ###标题 -> ### 标题
        block = re.sub(r"(?m)^(#{1,6})(\S)", r"\1 \2", block)
        # 去掉标题行前多余缩进，避免被当作代码块
        block = re.sub(r"(?m)^[ \t]+(#{1,6}\s*)", r"\1", block)

        # 标题前补空行，避免和正文粘连（仅匹配“正文后紧接标题”）
        block = re.sub(r"([^\n])\s*(#{1,6}\s*\S)", r"\1\n\n\2", block)
        # 标题后补空行，避免后续正文/表格粘连导致标题失效
        block = re.sub(r"(?m)^(#{1,6}\s*[^\n]+)\n(?!\n)", r"\1\n\n", block)

        # 标题和表格同行时拆行：### 评分|... -> ### 评分 \n |...
        block = re.sub(r"(?m)^(#{1,6}[^\n|]+?)\s*(\|)", r"\1\n\2", block)

        # 行内分隔线前后补换行（避免误伤表格分隔线 |---|）
        block = re.sub(r"(?<![|\n-])---(?![|\n-])", r"\n---\n", block)

        # 句号后接编号列表时补换行
        block = re.sub(r"([。；;：:])\s*(\d+\.\s*)", r"\1\n\2", block)
        block = re.sub(r"([^\n\s])(\d+\.\s*)", r"\1\n\2", block)

        # 句号后接无序列表时补换行
        block = re.sub(r"([。；;：:\.])\s*-(?!-)\s*", r"\1\n- ", block)
        block = re.sub(r"([^\n\s-])-\s+", r"\1\n- ", block)
        block = re.sub(r"(?m)^-(?!-)(\S)", r"- \1", block)

        # 压缩过多空行
        block = re.sub(r"\n{3,}", "\n\n", block)
        return block.strip()

    # 只在非代码块区域做文本修复，避免破坏代码。
    parts = normalized.split("```")
    for i in range(0, len(parts), 2):
        parts[i] = _normalize_prose_block(parts[i])
    normalized = "```".join(parts)

    return normalized.strip()


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
    cl.user_session.set("pending_interrupt", None)
    
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
    pending_interrupt = cl.user_session.get("pending_interrupt")
    
    if not runner:
        await cl.Message(content="❌ Runner 未初始化").send()
        return
    
    # 创建响应消息 (延迟发送，确保 Thinking 在前)
    msg = None

    user_content_for_history = message.content or ""

    # 准备输入（支持 interrupt 恢复 + 文件上传）
    if pending_interrupt is not None:
        input_data = {
            "session_id": session_id,
            "resume": True,
            "input": _parse_resume_input(message.content),
        }
        user_content_for_history = f"[RESUME] {message.content}"
        cl.user_session.set("pending_interrupt", None)
    else:
        files, upload_prompt = _extract_upload_context(message)
        user_input = message.content or ""
        if upload_prompt:
            user_input = f"{user_input}\n\n{upload_prompt}".strip() if user_input else upload_prompt
            if not user_content_for_history.strip():
                user_content_for_history = f"[上传了 {len(files)} 个文件]"

        input_data = {
            "input": user_input,
            "session_id": session_id,
            "history": history.copy(),  # 复制一份避免被修改
        }
        if files:
            # 透传给 Runner，供 Agent 按需读取
            input_data["files"] = files

    # Debug: 打印 history 长度
    print(
        f"[Chainlit] History length: {len(history)}, Session: {session_id}, "
        f"resume={bool(pending_interrupt)}"
    )
    
    full_response = ""
    thinking_content = ""
    thinking_step = None
    active_tool_steps = {}  # type: dict[str, cl.Step]
    interrupted = False
    
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
                delta = chunk.get("delta", "")
                thinking_content += delta
                
                if thinking_step is None:
                    thinking_step = cl.Step(name="🧠 Thinking", type="llm")
                    await thinking_step.__aenter__()
                
                await thinking_step.stream_token(delta)
                
            elif chunk_type == "tool_call":
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

            elif chunk_type == "interrupt":
                if msg:
                    await msg.update()
                    msg = None
                    full_response = ""

                if thinking_step:
                    thinking_step.output = thinking_content
                    await thinking_step.__aexit__(None, None, None)
                    thinking_step = None

                for step in active_tool_steps.values():
                    await step.__aexit__(None, None, None)
                active_tool_steps = {}

                interrupt_info = chunk.get("interrupt_info")
                full_response = _format_interrupt_prompt(interrupt_info)
                cl.user_session.set("pending_interrupt", interrupt_info)

                msg = cl.Message(content=full_response)
                await msg.send()
                interrupted = True
                break
        
        if thinking_step:
            thinking_step.output = thinking_content
            await thinking_step.__aexit__(None, None, None)
            
        # 关闭所有未关闭的工具 step
        for step in active_tool_steps.values():
            await step.__aexit__(None, None, None)
        
        full_response = _normalize_markdown(full_response)

        if not interrupted:
            if msg is None:
                 # 如果没有任何输出，发送空消息或 final response (已处理)
                 msg = cl.Message(content=full_response)
                 await msg.send()
            else:
                msg.content = full_response
                await msg.update()
        
        # 更新对话历史
        history.append({"role": "user", "content": user_content_for_history})
        history.append({"role": "assistant", "content": full_response})
        cl.user_session.set("history", history)
        
    except Exception as e:
        if thinking_step:
            try:
                await thinking_step.__aexit__(None, None, None)
            except:
                pass
        for step in active_tool_steps.values():
            try:
                await step.__aexit__(None, None, None)
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
