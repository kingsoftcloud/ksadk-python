#!/usr/bin/env python3
"""Generate public README and documentation visual assets."""

from __future__ import annotations

import io
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from rich.ansi import AnsiDecoder
from rich.console import Console
from rich.terminal_theme import TerminalTheme


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "public-docs" / "assets"
ARCH_SVG = ASSETS_DIR / "ksadk-runtime-architecture.svg"
ARCH_PNG = ASSETS_DIR / "ksadk-runtime-architecture.png"
HERO_PNG = ASSETS_DIR / "ksadk-runtime-platform-hero.png"
DEMO_GIF = ASSETS_DIR / "ksadk-local-debugging-demo.gif"
WEB_UI_SCREENSHOT = ASSETS_DIR / "ksadk-web-ui-screenshot.png"

CLI_SCREENSHOT_THEME = TerminalTheme(
    background=(255, 255, 255),
    foreground=(34, 34, 34),
    normal=[
        (34, 34, 34),
        (220, 38, 38),
        (0, 128, 96),
        (128, 128, 0),
        (0, 120, 140),
        (160, 64, 160),
        (0, 128, 160),
        (120, 120, 120),
    ],
    bright=[
        (0, 0, 0),
        (255, 87, 51),
        (0, 150, 110),
        (255, 193, 7),
        (0, 140, 170),
        (180, 80, 180),
        (0, 150, 180),
        (80, 80, 80),
    ],
)


def generate_architecture_svg() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    svg = """<svg viewBox="0 0 1400 960" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
  <title id="title">KsADK Agent Runtime Platform 架构图</title>
  <desc id="desc">从 Agent 代码到 KsADK SDK、统一运行时、Skill Runtime、Workspace、Sandbox、Memory Knowledge、AgentEngine、Serverless、Hermes 和 OpenClaw 的运行链路。</desc>
  <defs>
    <pattern id="grid" width="36" height="36" patternUnits="userSpaceOnUse">
      <path d="M 36 0 L 0 0 0 36" fill="none" stroke="#233047" stroke-width="0.6"/>
    </pattern>
    <marker id="arrow-cyan" markerWidth="12" markerHeight="8" refX="11" refY="4" orient="auto">
      <polygon points="0 0, 12 4, 0 8" fill="#22d3ee"/>
    </marker>
    <marker id="arrow-emerald" markerWidth="12" markerHeight="8" refX="11" refY="4" orient="auto">
      <polygon points="0 0, 12 4, 0 8" fill="#34d399"/>
    </marker>
    <marker id="arrow-slate" markerWidth="12" markerHeight="8" refX="11" refY="4" orient="auto">
      <polygon points="0 0, 12 4, 0 8" fill="#94a3b8"/>
    </marker>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="12" flood-color="#020617" flood-opacity="0.35"/>
    </filter>
    <style>
      text { font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", "Arial Unicode MS", sans-serif; }
      .title { fill: #f8fafc; font-size: 32px; font-weight: 700; }
      .subtitle { fill: #94a3b8; font-size: 16px; }
      .section { fill: #cbd5e1; font-size: 13px; font-weight: 700; letter-spacing: 1px; }
      .label { fill: #f8fafc; font-size: 19px; font-weight: 700; }
      .sub { fill: #aebacf; font-size: 13px; }
      .tiny { fill: #8da0bb; font-size: 12px; }
      .chip { fill: #cbd5e1; font-size: 12px; font-weight: 700; }
    </style>
  </defs>
  <rect width="1400" height="960" fill="#0b1220"/>
  <rect width="1400" height="960" fill="url(#grid)" opacity="0.5"/>
  <rect x="34" y="34" width="1332" height="892" rx="28" fill="#0f172a" stroke="#27364f" stroke-width="1.5"/>

  <text x="76" y="86" class="title">KsADK Agent Runtime Platform</text>
  <text x="76" y="116" class="subtitle">一次构建 Agent，到处运行：统一开发、调试、运行、沙箱、部署和观测体验</text>

  <rect x="84" y="164" width="1232" height="104" rx="18" fill="#0b1220" stroke="#334155"/>
  <text x="108" y="191" class="section">AGENT CODE</text>
  <g filter="url(#shadow)">
    <rect x="124" y="210" width="210" height="38" rx="10" fill="#082f49" stroke="#38bdf8"/>
    <text x="229" y="235" class="chip" text-anchor="middle">Google ADK</text>
    <rect x="374" y="210" width="210" height="38" rx="10" fill="#052e2b" stroke="#2dd4bf"/>
    <text x="479" y="235" class="chip" text-anchor="middle">LangGraph</text>
    <rect x="624" y="210" width="210" height="38" rx="10" fill="#3b2f12" stroke="#fbbf24"/>
    <text x="729" y="235" class="chip" text-anchor="middle">LangChain</text>
    <rect x="874" y="210" width="210" height="38" rx="10" fill="#312e81" stroke="#a78bfa"/>
    <text x="979" y="235" class="chip" text-anchor="middle">DeepAgents</text>
  </g>

  <path d="M 700 268 L 700 315" stroke="#22d3ee" stroke-width="2.5" marker-end="url(#arrow-cyan)"/>

  <g filter="url(#shadow)">
    <rect x="440" y="320" width="520" height="92" rx="18" fill="#082f49" stroke="#22d3ee" stroke-width="2"/>
    <text x="700" y="355" class="label" text-anchor="middle">KsADK SDK</text>
    <text x="700" y="382" class="sub" text-anchor="middle">Runner 适配 / 配置管理 / Toolsets / 项目打包</text>
  </g>

  <path d="M 700 412 L 700 455" stroke="#22d3ee" stroke-width="2.5" marker-end="url(#arrow-cyan)"/>

  <g filter="url(#shadow)">
    <rect x="378" y="462" width="644" height="118" rx="22" fill="#064e3b" stroke="#34d399" stroke-width="2.2"/>
    <text x="700" y="500" class="label" text-anchor="middle">统一运行时</text>
    <text x="700" y="529" class="sub" text-anchor="middle">CLI / Browser Web UI / OpenAI-Compatible API / Streaming Sessions</text>
    <text x="700" y="554" class="tiny" text-anchor="middle">本地开发时即验证部署后的运行边界</text>
  </g>

  <path d="M 700 580 L 700 630" stroke="#34d399" stroke-width="2.5" marker-end="url(#arrow-emerald)"/>
  <path d="M 700 610 C 482 610 342 630 228 669" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>
  <path d="M 700 610 C 560 618 492 637 454 669" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>
  <path d="M 700 610 C 840 618 908 637 946 669" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>
  <path d="M 700 610 C 918 610 1058 630 1172 669" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>

  <g filter="url(#shadow)">
    <rect x="100" y="676" width="236" height="86" rx="16" fill="#1e1b4b" stroke="#a78bfa" stroke-width="1.7"/>
    <text x="218" y="710" class="label" font-size="17" text-anchor="middle">Skill Runtime</text>
    <text x="218" y="737" class="sub" text-anchor="middle">Skill Space / workflow</text>

    <rect x="390" y="676" width="236" height="86" rx="16" fill="#3b2f12" stroke="#fbbf24" stroke-width="1.7"/>
    <text x="508" y="710" class="label" font-size="17" text-anchor="middle">Workspace</text>
    <text x="508" y="737" class="sub" text-anchor="middle">会话文件 / artifacts</text>

    <rect x="774" y="676" width="236" height="86" rx="16" fill="#4a1d2f" stroke="#fb7185" stroke-width="1.7"/>
    <text x="892" y="710" class="label" font-size="17" text-anchor="middle">Sandbox</text>
    <text x="892" y="737" class="sub" text-anchor="middle">隔离命令 / 代码执行</text>

    <rect x="1064" y="676" width="236" height="86" rx="16" fill="#063440" stroke="#67e8f9" stroke-width="1.7"/>
    <text x="1182" y="710" class="label" font-size="17" text-anchor="middle">Memory &amp; Knowledge</text>
    <text x="1182" y="737" class="sub" text-anchor="middle">长期记忆 / 知识库</text>
  </g>

  <path d="M 218 762 C 260 814 444 806 536 826" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>
  <path d="M 508 762 C 536 804 596 814 626 826" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>
  <path d="M 892 762 C 864 804 804 814 774 826" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>
  <path d="M 1182 762 C 1140 814 956 806 864 826" stroke="#94a3b8" stroke-width="1.8" fill="none" marker-end="url(#arrow-slate)"/>

  <g filter="url(#shadow)">
    <rect x="500" y="814" width="400" height="72" rx="18" fill="#172554" stroke="#60a5fa" stroke-width="2"/>
    <text x="700" y="844" class="label" text-anchor="middle">AgentEngine</text>
    <text x="700" y="870" class="sub" text-anchor="middle">远端运行、服务入口与平台能力</text>
  </g>

  <path d="M 700 886 L 700 905" stroke="#60a5fa" stroke-width="2.4" marker-end="url(#arrow-cyan)"/>

  <g filter="url(#shadow)">
    <rect x="470" y="912" width="460" height="34" rx="14" fill="#111827" stroke="#64748b" stroke-width="1.5"/>
    <text x="700" y="935" class="label" font-size="16" text-anchor="middle">Serverless / Hermes / OpenClaw Runtime</text>
  </g>
</svg>
"""
    ARCH_SVG.write_text(svg, encoding="utf-8")


def render_architecture_png() -> None:
    if ARCH_PNG.exists() and os.environ.get("KSADK_REGENERATE_ARCHITECTURE_PNG") != "1":
        return
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise RuntimeError("rsvg-convert is required to render architecture PNG")
    subprocess.run(
        [converter, str(ARCH_SVG), "--width", "1600", "--output", str(ARCH_PNG)],
        check=True,
    )


def _capture_cli_help_plain() -> str:
    env = os.environ.copy()
    env.pop("NO_COLOR", None)
    env.pop("AGENTENGINE_NO_COLOR", None)
    env["AGENTENGINE_OUTPUT_MODE"] = "pretty"
    env["COLUMNS"] = "120"
    completed = subprocess.run(
        [sys.executable, "-m", "ksadk.cli", "-h"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return completed.stdout


def _capture_cli_help_ansi() -> str:
    env = os.environ.copy()
    env.pop("NO_COLOR", None)
    env.pop("AGENTENGINE_NO_COLOR", None)
    env["AGENTENGINE_OUTPUT_MODE"] = "pretty"
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = "120"

    try:
        import pty

        master_fd, slave_fd = pty.openpty()
    except (ImportError, OSError):
        return _capture_cli_help_plain()

    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "ksadk.cli", "-h"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    chunks: list[bytes] = []
    try:
        while True:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)

            if process.poll() is not None:
                while True:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                break
    finally:
        os.close(master_fd)

    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, [sys.executable, "-m", "ksadk.cli", "-h"])
    return b"".join(chunks).decode("utf-8", "replace")


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def _trim_cli_help_for_readme(output: str) -> str:
    lines = output.replace("\r\n", "\n").splitlines()
    selected: list[str] = []
    for line in lines:
        plain = _strip_ansi(line)
        if "可用命令" in plain or "Available Commands" in plain:
            break
        selected.append(line)
    return "\n".join(selected).rstrip() + "\n"


def generate_hero_png() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise RuntimeError("rsvg-convert is required to render CLI screenshot PNG")

    output = _trim_cli_help_for_readme(_capture_cli_help_ansi())
    ansi = "\x1b[1;30m$ agentengine -h\x1b[0m\n" + output
    console = Console(
        record=True,
        width=118,
        force_terminal=True,
        color_system="truecolor",
        file=io.StringIO(),
        highlight=False,
    )
    decoder = AnsiDecoder()
    for line in decoder.decode(ansi):
        console.print(line, markup=False, highlight=False)

    svg = console.export_svg(title="agentengine -h", theme=CLI_SCREENSHOT_THEME)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".svg",
            prefix="ksadk-cli-help-",
            dir=ASSETS_DIR,
            delete=False,
        ) as temp_file:
            temp_file.write(svg)
            temp_path = Path(temp_file.name)
        subprocess.run(
            [converter, str(temp_path), "--width", "1600", "--output", str(HERO_PNG)],
            check=True,
        )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _find_chromium_executable() -> str | None:
    explicit_path = os.environ.get("KSADK_ASSET_CHROMIUM")
    if explicit_path and Path(explicit_path).is_file():
        return explicit_path

    candidates: list[Path] = []
    cache_roots = [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for cache_root in cache_roots:
        candidates.extend(
            cache_root.glob(
                "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        candidates.extend(
            cache_root.glob(
                "chromium-*/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        candidates.extend(cache_root.glob("chromium-*/chrome-linux/chrome"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    for executable_name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
    ):
        resolved = shutil.which(executable_name)
        if resolved:
            return resolved
    return None


class _PublicDemoRunner:
    """用于公开资产生成的 deterministic runner，不连接外部模型或云环境。"""

    def __init__(self):
        from ksadk.runners.base_runner import BaseRunner

        class Runner(BaseRunner):
            def __init__(self):
                super().__init__(
                    detection_result=SimpleNamespace(
                        name="runtime-platform-demo",
                        description="KsADK 真实 Web UI 演示",
                        type=SimpleNamespace(value="langgraph"),
                    ),
                    project_dir=str(ROOT),
                )

            def load_agent(self) -> None:
                return None

            async def invoke(self, input_data: dict) -> dict:
                return {
                    "output": (
                        "KsADK 已完成本地调试检查：运行时、Workspace、Sandbox "
                        "与工具调用链路均可在 Web UI 中观察。"
                    )
                }

            async def stream(self, input_data: dict):
                import asyncio

                yield {
                    "type": "tool_call",
                    "tool_name": "workspace_status",
                    "tool_args": {"path": "/workspace", "include_artifacts": True},
                    "status": "running",
                }
                await asyncio.sleep(0.45)
                yield {
                    "type": "tool_result",
                    "tool_name": "workspace_status",
                    "tool_output": '{"workspace":"ready","artifacts":3,"sandbox":"enabled"}',
                }
                await asyncio.sleep(0.45)
                yield {
                    "type": "thinking",
                    "delta": "正在检查 Skill Runtime、Workspace、Sandbox 和长期记忆配置边界。",
                }
                await asyncio.sleep(0.45)
                for delta in (
                    "KsADK 已接入 LangGraph Runner，",
                    "本地 Web UI 正在通过 Responses 流式协议返回结果。",
                    "\n\n- Workspace：可浏览会话文件和 artifacts",
                    "\n- Sandbox：支持隔离命令执行",
                    "\n- Skills：未配置 Skill Space 时会明确降级，不伪造工具结果",
                ):
                    yield {"type": "text", "delta": delta}
                    await asyncio.sleep(0.35)
                yield {
                    "type": "responses_output",
                    "response_id": "resp_public_demo",
                    "output": [
                        {
                            "id": "call_workspace_status",
                            "type": "function_call",
                            "name": "workspace_status",
                            "arguments": '{"path":"/workspace"}',
                        }
                    ],
                }
                yield {
                    "type": "final",
                    "output": (
                        "KsADK 已完成本地调试检查：运行时、Workspace、Sandbox "
                        "与工具调用链路均可在 Web UI 中观察。"
                    ),
                }

        self.runner = Runner()


@contextmanager
def _temporary_env(values: dict[str, str | None]):
    original = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _run_public_demo_server():
    import importlib

    import uvicorn
    from ksadk.sessions.in_memory import InMemorySessionService

    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    demo_runner = _PublicDemoRunner().runner
    server_app_module.resolve_session_service = lambda: service
    server_app_module.set_runner(demo_runner)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()

    config = uvicorn.Config(server_app_module.app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 8
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("KsADK Web UI demo server failed to start")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _save_web_ui_gif(frame_paths: list[Path]) -> None:
    frames: list[Image.Image] = []
    for frame_path in frame_paths:
        image = Image.open(frame_path).convert("RGB")
        target_width = 1100
        target_height = round(image.height * target_width / image.width)
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        frames.append(image)

    durations = [900, 900, 1000, 1200, 1800]
    frames[0].save(
        DEMO_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=durations[: len(frames)],
        loop=0,
        optimize=True,
    )


def generate_web_ui_assets() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is required to generate real Web UI assets. "
            "Install dev dependencies and run `python -m playwright install chromium`."
        ) from exc

    chromium = _find_chromium_executable()
    if chromium is None:
        raise RuntimeError(
            "Chromium is required to generate real Web UI assets. "
            "Set KSADK_ASSET_CHROMIUM or run `python -m playwright install chromium`."
        )

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ksadk-public-web-ui-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        frame_paths = [tmp_path / f"frame-{index}.png" for index in range(5)]
        with _temporary_env(
            {
                "AGENTENGINE_UI_DIR": str(tmp_path / ".agentengine" / "ui"),
                "OPENAI_MODEL_NAME": "gpt-4o-mini",
                "OPENAI_API_KEY": None,
                "OPENAI_BASE_URL": None,
                "OPENAI_API_BASE": None,
            }
        ):
            with _run_public_demo_server() as base_url:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=chromium, headless=True)
                    try:
                        page = browser.new_page(
                            viewport={"width": 1440, "height": 940},
                            device_scale_factor=1,
                        )
                        page.goto(f"{base_url}/chat")
                        page.wait_for_load_state("networkidle")
                        page.wait_for_selector("textarea")
                        page.screenshot(path=str(frame_paths[0]), full_page=False)
                        page.locator("textarea").fill(
                            "请检查这个 Agent 的工具、Workspace、Sandbox 和长期记忆配置"
                        )
                        page.screenshot(path=str(frame_paths[1]), full_page=False)
                        page.locator('button[type="submit"]').click()
                        page.wait_for_timeout(900)
                        page.screenshot(path=str(frame_paths[2]), full_page=False)
                        page.wait_for_timeout(1200)
                        page.screenshot(path=str(frame_paths[3]), full_page=False)
                        page.wait_for_function(
                            "() => document.body.innerText.includes('运行完成')",
                            timeout=10000,
                        )
                        page.screenshot(path=str(frame_paths[4]), full_page=False)
                    finally:
                        browser.close()

        final_image = Image.open(frame_paths[-1]).convert("RGB")
        final_image.save(WEB_UI_SCREENSHOT, optimize=True)
        _save_web_ui_gif(frame_paths)


def main() -> int:
    generate_hero_png()
    generate_architecture_svg()
    render_architecture_png()
    generate_web_ui_assets()
    print(f"generated {HERO_PNG.relative_to(ROOT)}")
    print(f"generated {ARCH_SVG.relative_to(ROOT)}")
    print(f"generated {ARCH_PNG.relative_to(ROOT)}")
    print(f"generated {WEB_UI_SCREENSHOT.relative_to(ROOT)}")
    print(f"generated {DEMO_GIF.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
