"""
BaseRunner - 运行时基类

所有框架 Runner 的抽象基类，定义统一接口
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Optional
import asyncio


class BaseRunner(ABC):
    """运行时基类"""

    def __init__(self, detection_result: Any, project_dir: str):
        self.detection_result = detection_result
        self.project_dir = project_dir
        self._agent = None

    @abstractmethod
    def load_agent(self) -> None:
        """加载 Agent"""
        pass

    @abstractmethod
    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """同步调用 Agent

        Args:
            input_data: 输入数据，通常包含 {"input": "用户消息"}

        Returns:
            输出数据，通常包含 {"output": "Agent 回复"}
        """
        pass

    @abstractmethod
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 Agent

        Args:
            input_data: 输入数据

        Yields:
            流式输出的数据块
        """
        pass

    def _get_console(self):
        """Lazy import rich console"""
        try:
            from rich.console import Console

            return Console()
        except ImportError:
            return None

    async def run_interactive(self, show_thinking: bool = False) -> None:
        """交互式运行

        在整个交互会话期间保持同一个 session_id 和对话历史，
        以便 Agent 可以记住上下文。

        Args:
            show_thinking: 是否展示模型思考过程（默认 False，折叠显示）
        """
        import uuid

        print("🤖 交互模式已启动，输入 'exit' 退出\n")

        # 创建一个持久的 session_id 和对话历史
        session_id = str(uuid.uuid4())[:8]
        history = []

        # 初始化输入历史记录 (支持按上键回填)
        try:
            from prompt_toolkit.history import InMemoryHistory

            input_history = InMemoryHistory()
        except ImportError:
            input_history = None

        while True:
            try:
                # 增加回合间距
                print()

                try:
                    import questionary
                    from questionary import Style

                    # 自定义样式：让输入文字保持默认颜色（去除默认的黄色）
                    style = Style(
                        [
                            ("qmark", "fg:green bold"),  # 问号绿色
                            ("question", "bold"),  # 提示语加粗
                            ("answer", "fg:#d3d3d3"),  # 输入内容使用浅灰色（避免默认黄色太刺眼）
                        ]
                    )

                    # 传递 history 对象以支持上键回填
                    kwargs = {}
                    if input_history:
                        kwargs["history"] = input_history

                    user_input = await questionary.text("👤 你:", style=style, **kwargs).ask_async()
                    if user_input is None:  # Ctrl+C
                        print("\n👋 再见!")
                        break
                    user_input = user_input.strip()
                except ImportError:
                    # Fallback if questionary is missing (should not happen in ksadk)
                    user_input = input("👤 你: ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit", "退出"):
                    print("\n👋 再见!")
                    break

                # 构建输入，包含 session_id 和 history
                input_data = {"input": user_input, "session_id": session_id, "history": history}

                response_text = ""
                thinking_text = ""
                console = self._get_console()

                if console:
                    from rich.live import Live
                    from rich.markdown import Markdown
                    from rich.panel import Panel
                    from rich.console import Group
                    from rich.rule import Rule

                    print("🤖 助手: ")
                    print()

                    with Live(
                        Markdown(""),
                        console=console,
                        refresh_per_second=12,
                        vertical_overflow="visible",
                    ) as live:
                        async for chunk in self.stream(input_data):
                            chunk_type = chunk.get("type", "text")

                            if chunk_type == "thinking":
                                if show_thinking:
                                    text = chunk.get("delta", "")
                                    if text:
                                        thinking_text += text
                                        thinking_panel = Panel(
                                            Markdown(thinking_text),
                                            title="💭 思考中...",
                                            border_style="dim",
                                            expand=False,
                                        )
                                        combined = Group(thinking_panel, Markdown(response_text))
                                        live.update(combined)
                                continue

                            text = ""
                            if "output" in chunk:
                                text = chunk["output"]
                            elif "delta" in chunk:
                                text = chunk["delta"]

                            if text:
                                response_text += text
                                if thinking_text and show_thinking:
                                    # 思考结束，显示折叠状态 (使用 Rule 代替 Panel)
                                    thinking_collapsed = Rule(
                                        title="💭 思考过程 (已折叠)", style="dim"
                                    )
                                    combined = Group(thinking_collapsed, Markdown(response_text))
                                    live.update(combined)
                                else:
                                    live.update(Markdown(response_text))
                else:
                    print("🤖 助手: ", end="", flush=True)
                    thinking_prefix_shown = False

                    async for chunk in self.stream(input_data):
                        chunk_type = chunk.get("type", "text")

                        if chunk_type == "thinking":
                            if show_thinking:
                                if not thinking_prefix_shown:
                                    print("\n💭 [思考中...] ", end="", flush=True)
                                    thinking_prefix_shown = True
                                text = chunk.get("delta", "")
                                if text:
                                    print(text, end="", flush=True)
                            continue

                        if thinking_prefix_shown:
                            print("\n\n", end="", flush=True)
                            thinking_prefix_shown = False

                        if "output" in chunk:
                            text = chunk["output"]
                            print(text, end="", flush=True)
                            response_text += text
                        elif "delta" in chunk:
                            text = chunk["delta"]
                            print(text, end="", flush=True)
                            response_text += text
                        elif "delta" in chunk:
                            text = chunk["delta"]
                            print(text, end="", flush=True)
                            response_text += text

                if not response_text:
                    # 如果没有流式输出，使用同步调用
                    result = await self.invoke(input_data)
                    response_text = result.get("output", "(无响应)")
                    print(response_text)
                else:
                    print()  # 换行

                # 更新对话历史
                history.append({"role": "user", "content": user_input})
                history.append({"role": "model", "content": response_text})

                print()

            except KeyboardInterrupt:
                print("\n\n👋 再见!")
                break
            except Exception as e:
                import traceback

                traceback.print_exc()
                print(f"\n❌ 错误: {e}\n")

    def run_server(self, port: int = 8000) -> None:
        """启动 HTTP Server"""
        from ksadk.server import app, set_runner
        import uvicorn

        set_runner(self)
        uvicorn.run(app, host="0.0.0.0", port=port)
