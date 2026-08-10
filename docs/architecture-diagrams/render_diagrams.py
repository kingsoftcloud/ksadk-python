from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
FONT_PATH = Path("/System/Library/Fonts/STHeiti Light.ttc")

BG = "#FAFBFD"
INK = "#1F2937"
MUTED = "#5F6B7A"
LINE = "#637083"
BLUE = "#E8F2FF"
BLUE_LINE = "#3B78C4"
ORANGE = "#FFF3DE"
ORANGE_LINE = "#D98918"
GREEN = "#E9F7ED"
GREEN_LINE = "#3B8F58"
PURPLE = "#F2EAFE"
PURPLE_LINE = "#8559B5"
RED = "#FDECEC"
RED_LINE = "#C24B4B"
GRAY = "#F1F3F5"
GRAY_LINE = "#7A8491"
WHITE = "#FFFFFF"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size=size)


TITLE = font(44)
SUBTITLE = font(24)
GROUP = font(28)
BODY = font(23)
SMALL = font(19)
TINY = font(16)


class Diagram:
    def __init__(self, title: str, subtitle: str = "", size: tuple[int, int] = (1920, 1080)):
        self.image = Image.new("RGB", size, BG)
        self.draw = ImageDraw.Draw(self.image)
        self.w, self.h = size
        self.draw.text((self.w / 2, 34), title, font=TITLE, fill=INK, anchor="ma")
        if subtitle:
            self.draw.text((self.w / 2, 92), subtitle, font=SUBTITLE, fill=MUTED, anchor="ma")

    def text(self, xy: tuple[float, float], value: str, *, f=BODY, fill=INK, anchor="mm"):
        self.draw.multiline_text(xy, value, font=f, fill=fill, anchor=anchor, align="center", spacing=8)

    def group(self, rect: tuple[int, int, int, int], title: str, *, fill=WHITE, outline=GRAY_LINE):
        x1, y1, x2, y2 = rect
        self.draw.rounded_rectangle(rect, radius=24, fill=fill, outline=outline, width=3)
        self.draw.rectangle((x1 + 20, y1 - 14, x1 + 34 + self.draw.textlength(title, font=GROUP), y1 + 22), fill=BG)
        self.draw.text((x1 + 28, y1 + 4), title, font=GROUP, fill=outline, anchor="lm")

    def box(
        self,
        rect: tuple[int, int, int, int],
        text: str,
        *,
        fill=BLUE,
        outline=BLUE_LINE,
        f=BODY,
        radius=20,
        width=3,
    ):
        x1, y1, x2, y2 = rect
        self.draw.rounded_rectangle(rect, radius=radius, fill=fill, outline=outline, width=width)
        self.text(((x1 + x2) / 2, (y1 + y2) / 2), text, f=f)

    def pill(self, center: tuple[int, int], text: str, *, fill=GREEN, outline=GREEN_LINE, width=340):
        x, y = center
        self.box((x - width // 2, y - 42, x + width // 2, y + 42), text, fill=fill, outline=outline)

    def arrow(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        color=LINE,
        width=4,
        label: str | None = None,
        label_offset: tuple[int, int] = (0, -16),
        dashed=False,
    ):
        if dashed:
            self._dashed_line(start, end, color=color, width=width)
        else:
            self.draw.line((start, end), fill=color, width=width)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        length = 16
        spread = 0.6
        points = [
            end,
            (end[0] - length * math.cos(angle - spread), end[1] - length * math.sin(angle - spread)),
            (end[0] - length * math.cos(angle + spread), end[1] - length * math.sin(angle + spread)),
        ]
        self.draw.polygon(points, fill=color)
        if label:
            mx = (start[0] + end[0]) / 2 + label_offset[0]
            my = (start[1] + end[1]) / 2 + label_offset[1]
            bbox = self.draw.textbbox((mx, my), label, font=SMALL, anchor="mm")
            self.draw.rounded_rectangle((bbox[0] - 8, bbox[1] - 4, bbox[2] + 8, bbox[3] + 4), radius=8, fill=BG)
            self.draw.text((mx, my), label, font=SMALL, fill=MUTED, anchor="mm")

    def poly_arrow(self, points: list[tuple[int, int]], *, color=LINE, width=4, label: str | None = None):
        for a, b in zip(points, points[1:]):
            self.draw.line((a, b), fill=color, width=width)
        self.arrow(points[-2], points[-1], color=color, width=width, label=label)

    def _dashed_line(self, start, end, *, color, width):
        dx, dy = end[0] - start[0], end[1] - start[1]
        dist = max(1, math.hypot(dx, dy))
        ux, uy = dx / dist, dy / dist
        pos = 0.0
        while pos < dist:
            stop = min(pos + 12, dist)
            self.draw.line(
                (start[0] + ux * pos, start[1] + uy * pos, start[0] + ux * stop, start[1] + uy * stop),
                fill=color,
                width=width,
            )
            pos += 22

    def save(self, name: str):
        path = ROOT / name
        self.image.save(path, format="PNG", optimize=True)
        return path


def reference_map():
    d = Diagram(
        "参考架构思想如何收敛为 KsADK 设计",
        "参考通用工程模式，不复制第三方内部实现",
    )
    d.group((40, 145, 570, 1005), "参考项目与已验证思想", fill="#FFFDFC", outline=ORANGE_LINE)
    refs = [
        ("Claude Code 分析 / claw-code", "Prompt Sections\nCache-break / Contract Test"),
        ("OpenClaw / Hermes", "稳定 Prompt 与动态 Context\n主动压缩 / Working Context"),
        ("Letta / LangGraph / OpenHands", "Core Memory Block\n短期与长期边界 / Skill 按需加载"),
        ("Mem0 / RULER", "候选写入与冲突处理\n长上下文分级测试"),
    ]
    ys = [250, 445, 640, 835]
    for y, (a, b) in zip(ys, refs):
        d.box((80, y - 70, 530, y + 70), f"{a}\n{b}", fill=ORANGE, outline=ORANGE_LINE, f=SMALL)

    d.group((635, 145, 1135, 1005), "提炼后的设计原则", fill="#FCFDFF", outline=BLUE_LINE)
    principles = [
        "稳定指令与动态上下文分离\n所有覆盖规则显式化",
        "Transcript / WorkingState / LTM\n按生命周期严格分层",
        "先预算与确定性降载\n最后才做语义压缩",
        "多 Runner 统一合同\n不强行统一内部 Agent Loop",
    ]
    for y, text in zip(ys, principles):
        d.box((675, y - 70, 1095, y + 70), text, fill=BLUE, outline=BLUE_LINE, f=SMALL)

    d.group((1200, 145, 1880, 1005), "KsADK 最终抽象", fill="#FCFFFD", outline=GREEN_LINE)
    outputs = [
        "CompiledPrompt\n版本、Hash、稳定前缀、覆盖规则",
        "ContextPlan\n预算、选择、裁剪、Projection、Trace",
        "ContextCheckpoint + WorkingState\n长 Session 连续性与 append-only 审计",
        "MemoryProvider + Candidate\n跨 Session 事实、版本、Scope、删除",
        "Runner ContextCapabilities\nOwnership、精度、Conformance",
    ]
    out_y = [225, 395, 565, 735, 905]
    for y, text in zip(out_y, outputs):
        d.box((1245, y - 60, 1835, y + 60), text, fill=GREEN, outline=GREEN_LINE, f=SMALL)

    for y in ys:
        d.arrow((530, y), (675, y), color=ORANGE_LINE)
    mappings = [(250, 225), (250, 905), (445, 395), (445, 565), (640, 735), (640, 905), (835, 735), (835, 395)]
    for sy, ey in mappings:
        d.arrow((1095, sy), (1245, ey), color=BLUE_LINE, width=3)
    d.save("01-reference-to-ksadk.png")


def overall_architecture():
    d = Diagram("KsADK Prompt、Context 与 Memory 总体架构", "双构建、统一 AgentVersion、统一云部署", size=(1920, 1280))
    groups = [
        ("入口层", 150, 300, [("CLI / Studio", GRAY, GRAY_LINE), ("KsADK SDK / Agent SDK", GRAY, GRAY_LINE), ("Hosted API / Gateway / A2A", GRAY, GRAY_LINE)]),
        ("AgentVersion 与部署控制层", 340, 505, [("AgentVersion\nPrompt Source / 默认策略 / Hash", ORANGE, ORANGE_LINE), ("Deployment\nRuntime / Model / Provider Binding", ORANGE, ORANGE_LINE), ("Platform Policy\n安全边界 / Override 范围", ORANGE, ORANGE_LINE)]),
        ("KsADK Runtime Preparation", 545, 745, [("Prompt Compiler", BLUE, BLUE_LINE), ("Capability Resolver", BLUE, BLUE_LINE), ("Context Contributors", BLUE, BLUE_LINE), ("Context Engine / Planner", BLUE, BLUE_LINE), ("Runner Projection", BLUE, BLUE_LINE)]),
        ("Runner / Agent Runtime", 785, 955, [("ksadk_hosted\n完整组装与压缩", PURPLE, PURPLE_LINE), ("framework_assisted\nADK / LangGraph / LangChain", PURPLE, PURPLE_LINE), ("native_runtime\nCodex / Claude SDK / Hermes", PURPLE, PURPLE_LINE)]),
        ("Session、Provider 与治理", 995, 1210, [("Session Transcript\nappend-only", GREEN, GREEN_LINE), ("Checkpoint\nSummary + WorkingState", GREEN, GREEN_LINE), ("MemoryProvider\nCore / Recall / Candidate", GREEN, GREEN_LINE), ("Artifact Store\n大结果与附件", GREEN, GREEN_LINE), ("Trace / Cache / Audit", GREEN, GREEN_LINE)]),
    ]
    centers: list[list[tuple[int, int]]] = []
    for title, y1, y2, items in groups:
        d.group((35, y1, 1885, y2), title)
        n = len(items)
        gap = 24
        inner_x1, inner_x2 = 75, 1845
        bw = int((inner_x2 - inner_x1 - gap * (n - 1)) / n)
        row = []
        for i, (text, fill, outline) in enumerate(items):
            x1 = inner_x1 + i * (bw + gap)
            d.box((x1, y1 + 38, x1 + bw, y2 - 24), text, fill=fill, outline=outline, f=SMALL)
            row.append((x1 + bw // 2, (y1 + y2) // 2))
        centers.append(row)
    for a, b in zip(centers, centers[1:]):
        for i, src in enumerate(a):
            dst = b[round(i * (len(b) - 1) / max(1, len(a) - 1))]
            d.arrow((src[0], src[1] + 64), (dst[0], dst[1] - 64), width=3)
    d.save("02-ksadk-overall-architecture.png")


def runner_ownership():
    d = Diagram("多 Runner 的二维运行合同", "部署位置回答“在哪里运行”；Context Ownership 回答“谁拥有最终输入”")

    d.group((55, 145, 1865, 310), "轴一：DeploymentMode（生命周期与运行位置）", fill="#FCFDFF", outline=BLUE_LINE)
    deploy = [
        (95, 560, "local\n本机进程 / 本地容器"),
        (725, 1190, "ksadk_managed_cloud\n平台构建 / 扩缩容 / 运维"),
        (1355, 1820, "external_managed\n第三方托管 Runtime"),
    ]
    for x1, x2, text in deploy:
        d.box((x1, 190, x2, 275), text, fill=BLUE, outline=BLUE_LINE, f=SMALL)

    d.pill((960, 365), "Semantic Envelope：CompiledPrompt + ContextPlan + Policy", width=900)
    d.text((960, 420), "轴二：ContextIntegrationMode（Prompt / History / Memory / Compaction Ownership）", f=SMALL, fill=MUTED)

    cols = [
        (75, 560, "ksadk_hosted ＝ ksadk_owned", GREEN_LINE, GREEN, ["KsADK Agent Loop + Assembler", "KsADK History / Compaction", "最终输入可提供 exact 证据"]),
        (720, 1205, "framework_assisted", ORANGE_LINE, ORANGE, ["KsADK 提供语义信封", "Adapter → Instruction / State / Store", "框架组装最终输入"]),
        (1365, 1850, "native_runtime ＝ native_owned", PURPLE_LINE, PURPLE, ["版本化 Instructions / Resource Hook", "原生 Agent Loop / Thread / Compaction", "runtime_reported / opaque"]),
    ]
    for x1, x2, title, line, fill, items in cols:
        d.group((x1 - 20, 465, x2 + 20, 825), title, outline=line)
        ys = [545, 655, 765]
        for y, text in zip(ys, items):
            d.box((x1, y - 38, x2, y + 38), text, fill=fill, outline=line, f=SMALL)
        d.arrow(((x1 + x2) // 2, 583), ((x1 + x2) // 2, 617), color=line)
        d.arrow(((x1 + x2) // 2, 693), ((x1 + x2) // 2, 727), color=line)
        d.arrow((960, 407), ((x1 + x2) // 2, 507), color=line, width=3)

    d.box((80, 880, 610, 1005), "KsADK Harness\nmanaged_cloud + ksadk_hosted", fill=GREEN, outline=GREEN_LINE, f=SMALL)
    d.box((695, 880, 1225, 1005), "Managed Codex Runtime\nmanaged_cloud + native_runtime", fill=PURPLE, outline=PURPLE_LINE, f=SMALL)
    d.box((1310, 880, 1840, 1005), "核心门禁\n部署变化不得静默改变 Ownership", fill=RED, outline=RED_LINE, f=SMALL)
    d.arrow((610, 942), (695, 942), color=LINE, label="不是同一种接管")
    d.arrow((1225, 942), (1310, 942), color=RED_LINE)
    d.save("03-runner-context-ownership.png")


def context_assembly():
    d = Diagram("单次请求的 Context 组装与预算决策", "所有内容先成为有来源、有信任级别、有预算的 ContextItem")
    sources = ["CompiledPrompt", "Current Input", "Transcript / Checkpoint", "WorkingState", "Core / Recall Memory", "Skill / Tool Manifest", "Rules / Git / Attachment"]
    bw, gap, x0 = 235, 24, 70
    for i, text in enumerate(sources):
        x1 = x0 + i * (bw + gap)
        d.box((x1, 155, x1 + bw, 255), text, fill=GRAY, outline=GRAY_LINE, f=SMALL)
        d.arrow((x1 + bw // 2, 255), (960, 340), width=2)

    d.box((545, 340, 1375, 445), "ContextItem 候选集\nsource / trust / priority / tokens / provenance / group", fill=BLUE, outline=BLUE_LINE)
    stages = [
        ("单项预算\nTool Result / Attachment\nRule File", 180, 510),
        ("协议分组\nRound / Tool Pair\nApproval / Receipt", 555, 510),
        ("Required 锁定\nSafety / Current Input\nPending State", 930, 510),
        ("分区装箱\nPrompt / Working / History\nMemory / Resource", 1305, 510),
    ]
    for text, x, y in stages:
        d.box((x, y, x + 315, y + 140), text, fill=ORANGE, outline=ORANGE_LINE, f=TINY)
    d.arrow((960, 445), (337, 510), color=BLUE_LINE)
    for i in range(3):
        d.arrow((495 + i * 375, 580), (555 + i * 375, 580), color=ORANGE_LINE)

    d.box((450, 705, 790, 805), "超过 Soft Limit？", fill=RED, outline=RED_LINE)
    d.box((845, 705, 1265, 805), "确定性降载\nDedupe → Snip → Microcompact\n降低 Recall", fill=ORANGE, outline=ORANGE_LINE, f=TINY)
    d.box((1320, 705, 1660, 805), "仍超过 Hard Limit？", fill=RED, outline=RED_LINE)
    d.arrow((1462, 650), (620, 705), color=LINE)
    d.arrow((790, 755), (845, 755), color=RED_LINE, label="是")
    d.arrow((1265, 755), (1320, 755), color=ORANGE_LINE)

    d.box((220, 900, 590, 1000), "ContextPlan", fill=GREEN, outline=GREEN_LINE)
    d.box((775, 900, 1145, 1000), "Semantic / Emergency Compact\n仅 owner 允许时执行", fill=RED, outline=RED_LINE, f=SMALL)
    d.box((1330, 900, 1700, 1000), "Runner Projection → Runtime\nUsage / Cache Facts → Trace", fill=GREEN, outline=GREEN_LINE, f=SMALL)
    d.arrow((620, 805), (405, 900), color=GREEN_LINE, label="否")
    d.arrow((1490, 805), (960, 900), color=RED_LINE, label="是")
    d.arrow((1490, 805), (1515, 900), color=GREEN_LINE, label="否")
    d.poly_arrow([(960, 1000), (960, 1040), (405, 1040), (405, 1000)], color=RED_LINE, label="Re-plan")
    d.arrow((590, 950), (1330, 950), color=GREEN_LINE)
    d.save("04-context-assembly-and-budget.png")


def compaction_flow():
    d = Diagram("有序 Compaction、WorkingState 与恢复", "压缩只改变投影，不删除 append-only Transcript")
    d.box((60, 165, 360, 270), "Session Events\n用户 / 助手 / Tool / Approval", fill=GRAY, outline=GRAY_LINE, f=SMALL)
    d.box((430, 165, 730, 270), "按 API Round 分组\n识别 pinned state", fill=GRAY, outline=GRAY_LINE, f=SMALL)
    d.arrow((360, 217), (430, 217))

    stages = [
        (80, "L1 单项预算\n大 Tool Result → Artifact Ref"),
        (430, "L2 Snip\n移除被覆盖的旧投影"),
        (780, "L3 Microcompact\n冷轮次确定性摘要"),
        (1130, "Memory Flush\n仅提议跨 Session 事实"),
        (1480, "L4 Semantic Summary\n超时 / 熔断 / Extractive Fallback"),
    ]
    for x, text in stages:
        d.box((x, 375, x + 300, 500), text, fill=ORANGE, outline=ORANGE_LINE, f=SMALL)
    d.arrow((580, 270), (230, 375))
    for x in [380, 730, 1080, 1430]:
        d.arrow((x, 437), (x + 50, 437), color=ORANGE_LINE)

    d.box((410, 610, 840, 745), "ContextCheckpoint Summary\n过去发生了什么", fill=BLUE, outline=BLUE_LINE)
    d.box((1080, 610, 1510, 745), "WorkingState\n当前目标 / 阶段 / 下一步\n错误修正 / Pending State", fill=BLUE, outline=BLUE_LINE, f=SMALL)
    d.arrow((1630, 500), (625, 610), color=BLUE_LINE)
    d.arrow((1630, 500), (1295, 610), color=BLUE_LINE)
    d.box((685, 830, 1235, 950), "追加 ContextCheckpoint\nSummary + WorkingState + Stats + Seq Range", fill=GREEN, outline=GREEN_LINE)
    d.arrow((625, 745), (850, 830), color=GREEN_LINE)
    d.arrow((1295, 745), (1070, 830), color=GREEN_LINE)
    d.box((1320, 830, 1840, 950), "下轮重注入并继续\nSummary + WorkingState + Receipt + Artifact Ref", fill=GREEN, outline=GREEN_LINE, f=SMALL)
    d.arrow((1235, 890), (1320, 890), color=GREEN_LINE)

    d.box((60, 610, 330, 745), "原始 Events\nReplay / Audit / Evaluation", fill=GRAY, outline=GRAY_LINE, f=SMALL)
    d.arrow((210, 270), (195, 610), color=GRAY_LINE, dashed=True)
    d.text((335, 575), "原始事实始终保留", f=TINY, fill=MUTED)
    d.box((60, 830, 515, 950), "PTL Emergency Guard\n只允许一次 Retry；再次失败返回结构化错误", fill=RED, outline=RED_LINE, f=SMALL)
    d.arrow((285, 830), (230, 500), color=RED_LINE)
    d.save("05-compaction-working-state-recovery.png")


def memory_lifecycle():
    d = Diagram("Session 状态与长期记忆生命周期", "WorkingState 服务当前任务；Memory 只保存跨 Session 仍有效的事实", size=(1920, 1200))
    d.group((45, 150, 575, 835), "Session 级：当前任务连续性", outline=BLUE_LINE)
    d.box((95, 230, 525, 345), "Transcript\n完整 append-only 事实", fill=BLUE, outline=BLUE_LINE)
    d.box((95, 440, 525, 555), "Checkpoint Summary\n过去发生了什么", fill=BLUE, outline=BLUE_LINE)
    d.box((95, 650, 525, 775), "WorkingState\n现在做到哪、下一步是什么", fill=BLUE, outline=BLUE_LINE)
    d.arrow((310, 345), (310, 440), color=BLUE_LINE)
    d.arrow((310, 345), (310, 650), color=BLUE_LINE)

    d.group((650, 150, 1265, 835), "记忆加工：什么值得跨 Session", outline=ORANGE_LINE)
    steps = ["Candidate Extraction", "Policy\nSecret / PII / Scope", "去重 / 冲突 / 版本判断", "add / update / supersede\ndelete / reject"]
    ys = [230, 380, 530, 680]
    for y, text in zip(ys, steps):
        d.box((715, y, 1200, y + 100), text, fill=ORANGE, outline=ORANGE_LINE, f=SMALL)
    for y in [330, 480, 630]:
        d.arrow((958, y), (958, y + 50), color=ORANGE_LINE)
    d.arrow((525, 287), (715, 280), color=ORANGE_LINE)

    d.group((1340, 150, 1875, 835), "跨 Session：长期事实源", outline=GREEN_LINE)
    d.box((1395, 250, 1820, 370), "MemoryProvider\nLocal SQLite / HTTP / SDK", fill=GREEN, outline=GREEN_LINE)
    d.box((1395, 470, 1820, 575), "Core Memory\n少量稳定 Profile / Fact", fill=GREEN, outline=GREEN_LINE, f=SMALL)
    d.box((1395, 655, 1820, 760), "Recall Memory\n按查询召回 Fact / Episode", fill=GREEN, outline=GREEN_LINE, f=SMALL)
    d.arrow((1200, 730), (1395, 310), color=GREEN_LINE, label="commit")
    d.arrow((1607, 370), (1607, 470), color=GREEN_LINE)
    d.arrow((1607, 370), (1607, 655), color=GREEN_LINE)

    d.box((180, 910, 650, 1020), "下一次 Current Input + WorkingState", fill=PURPLE, outline=PURPLE_LINE)
    d.box((760, 910, 1240, 1020), "Memory Search\nscope / score / token budget", fill=PURPLE, outline=PURPLE_LINE)
    d.box((1350, 910, 1810, 1020), "Untrusted ContextItems → ContextPlan\nmemory_id / score / provenance", fill=PURPLE, outline=PURPLE_LINE, f=SMALL)
    d.arrow((650, 965), (760, 965), color=PURPLE_LINE)
    d.arrow((1240, 965), (1350, 965), color=PURPLE_LINE)
    d.arrow((1607, 760), (1580, 910), color=PURPLE_LINE)
    d.arrow((310, 775), (415, 910), color=PURPLE_LINE, label="仅当前 Session")
    d.box((690, 1080, 1310, 1160), "Provider 失败 → 结构化空结果 + Trace\n错误文本不进入模型", fill=RED, outline=RED_LINE, f=SMALL)
    d.save("06-memory-lifecycle.png")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    reference_map()
    overall_architecture()
    runner_ownership()
    context_assembly()
    compaction_flow()
    memory_lifecycle()


if __name__ == "__main__":
    main()
