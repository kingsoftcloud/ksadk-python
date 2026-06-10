#!/usr/bin/env python3
"""Generate public README and documentation visual assets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "public-docs" / "assets"
ARCH_SVG = ASSETS_DIR / "ksadk-runtime-architecture.svg"
ARCH_PNG = ASSETS_DIR / "ksadk-runtime-architecture.png"
DEMO_GIF = ASSETS_DIR / "ksadk-local-debugging-demo.gif"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


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
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise RuntimeError("rsvg-convert is required to render architecture PNG")
    subprocess.run(
        [converter, str(ARCH_SVG), "--width", "1600", "--output", str(ARCH_PNG)],
        check=True,
    )


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str, outline: str | None = None, width: int = 1, radius: int = 16) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int, fill: str = "#e5edf7", *, bold: bool = False) -> None:
    draw.text(xy, text, fill=fill, font=_font(size, bold=bold))


def _draw_window(draw: ImageDraw.ImageDraw) -> None:
    _rounded(draw, (36, 28, 924, 512), "#0f172a", "#2a3954", 2, 22)
    draw.ellipse((64, 56, 76, 68), fill="#fb7185")
    draw.ellipse((86, 56, 98, 68), fill="#fbbf24")
    draw.ellipse((108, 56, 120, 68), fill="#34d399")
    _text(draw, (144, 51), "agentengine web · 本地调试", 18, "#f8fafc", bold=True)


def _draw_terminal(draw: ImageDraw.ImageDraw, step: int) -> None:
    _rounded(draw, (62, 92, 376, 474), "#020617", "#334155", 1, 14)
    _text(draw, (84, 116), "$ pip install -U ksadk[all]", 15, "#a7f3d0")
    _text(draw, (84, 152), "$ agentengine init demo -f langgraph", 15, "#a7f3d0")
    _text(draw, (84, 188), "$ agentengine run -i", 15, "#a7f3d0")
    if step >= 1:
        _text(draw, (84, 234), "Runner: LangGraph", 15, "#93c5fd")
        _text(draw, (84, 264), "Model: OpenAI-compatible", 15, "#93c5fd")
    if step >= 2:
        _text(draw, (84, 310), "$ agentengine web .", 15, "#a7f3d0")
        _text(draw, (84, 340), "Web UI: http://127.0.0.1:8080", 15, "#fbbf24")
    if step >= 4:
        _text(draw, (84, 392), "tool_call: workspace_status", 15, "#f0abfc")
        _text(draw, (84, 422), "trace: exported via OTLP", 15, "#f0abfc")


def _draw_chat(draw: ImageDraw.ImageDraw, step: int) -> None:
    _rounded(draw, (410, 92, 898, 474), "#111827", "#334155", 1, 16)
    _text(draw, (436, 116), "Browser Debugging UI", 17, "#f8fafc", bold=True)
    _rounded(draw, (436, 154, 850, 202), "#1e293b", "#475569", 1, 12)
    _text(draw, (456, 168), "用户：检查工具、workspace 和部署边界", 15, "#e2e8f0")

    if step >= 1:
        _rounded(draw, (436, 226, 850, 306), "#082f49", "#38bdf8", 1, 12)
        content = "助手：已绑定 LangGraph Runner，正在读取 toolsets..."
        if step >= 3:
            content = "助手：已绑定 LangGraph Runner，并检测到 Skill / Workspace / Sandbox 配置边界。"
        _text(draw, (456, 244), content[:29], 15, "#e0f2fe")
        if len(content) > 29:
            _text(draw, (456, 272), content[29:], 15, "#e0f2fe")

    if step >= 2:
        _rounded(draw, (436, 330, 562, 366), "#052e2b", "#2dd4bf", 1, 10)
        _text(draw, (452, 338), "Streaming", 14, "#ccfbf1", bold=True)
        _rounded(draw, (580, 330, 704, 366), "#3b2f12", "#fbbf24", 1, 10)
        _text(draw, (598, 338), "Artifacts", 14, "#fef3c7", bold=True)
        _rounded(draw, (722, 330, 850, 366), "#1e1b4b", "#a78bfa", 1, 10)
        _text(draw, (742, 338), "Tracing", 14, "#ede9fe", bold=True)

    if step >= 4:
        _rounded(draw, (436, 386, 850, 448), "#172554", "#60a5fa", 1, 12)
        _text(draw, (456, 398), "工具结果：workspace 可用；未配置云 Skill Space", 14, "#dbeafe")
        _text(draw, (456, 422), "时给出明确降级说明，不伪造平台结果。", 14, "#dbeafe")


def generate_demo_gif() -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    for step in [0, 1, 2, 3, 4, 5, 5, 4, 3]:
        image = Image.new("RGB", (960, 540), "#0b1220")
        draw = ImageDraw.Draw(image)
        for x in range(0, 960, 32):
            draw.line((x, 0, x, 540), fill="#172033", width=1)
        for y in range(0, 540, 32):
            draw.line((0, y, 960, y), fill="#172033", width=1)
        _draw_window(draw)
        _draw_terminal(draw, step)
        _draw_chat(draw, step)
        _text(draw, (62, 492), "统一 CLI + Web UI + 工具调用 + Workspace + Tracing", 15, "#94a3b8")
        frames.append(image)
        durations.append(900 if step != 5 else 1200)
    frames[0].save(
        DEMO_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )


def main() -> int:
    generate_architecture_svg()
    render_architecture_png()
    generate_demo_gif()
    print(f"generated {ARCH_SVG.relative_to(ROOT)}")
    print(f"generated {ARCH_PNG.relative_to(ROOT)}")
    print(f"generated {DEMO_GIF.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
