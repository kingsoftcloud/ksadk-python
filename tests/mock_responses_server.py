"""Mock responses SSE server (aiohttp async) for ksadk TUI E2E.

支持并发连接、正确流式 SSE。场景:
- thinking_list: thinking + 有序列表 + 工具调用
- table: 表格
- long: 30 行测滚动
- tool_only: 只有工具调用(无正文)
用法: python tests/mock_responses_server.py [port] [scenario]
"""
from __future__ import annotations

import asyncio
import json
import sys
from aiohttp import web


async def _send_events(response, events):
    """逐事件写 SSE，间隔小延迟模拟流式。"""
    for ev in events:
        await response.write(f"event: {ev[0]}\ndata: {json.dumps(ev[1])}\n\n".encode())
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)  # 让 client 读完


def _reasoning(text):
    return ("response.reasoning.delta", {"type": "response.reasoning.delta", "delta": text})


def _text(text):
    return ("response.output_text.delta", {"type": "response.output_text.delta", "delta": text})


def _completed(model="glm-5.2", total=12100):
    return ("response.completed", {
        "type": "response.completed",
        "response": {"model": model, "output": [], "usage": {"total_tokens": total, "last_usage": {"total_tokens": total}}},
    })


def scenario_thinking_list():
    evs = [_reasoning("The user is asking about skills. I should list them.")]
    # 同时用 content_part.delta 发 reasoning（对标 glm 真机：partType=reasoning）
    evs.append(("response.content_part.delta", {"type": "response.content_part.delta", "part": {"type": "reasoning"}, "delta": {"text": "Let me check the skills list."}}))
    parts = [
        "好的，我来帮你查看技能！\n\n",
        "当前有 4 个技能：\n\n",
        "1. ppt-translator — 将 PPT 翻译为任意语言\n",
        "2. kingsoft-ppt-dark-botanical — 创建深色科技风 HTML 演示文稿\n",
        "3. self-improvement — 捕获学习经验实现自我改进\n",
        "4. skill-creator — 技能创建向导\n",
    ]
    for p in parts:
        evs.append(_text(p))
    evs.append(("response.output_item.added", {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1", "name": "list_skills", "arguments": ""}}))
    evs.append(("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": "fc_1", "arguments": "{}"}))
    evs.append(("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "function_call_output", "call_id": "fc_1", "output": "found 4 skills"}}))
    evs.append(_completed())
    return evs


def scenario_long():
    return [_text(f"行 {i:02d} 内容占位占位占位占位占位\n") for i in range(30)] + [_completed(total=21000)]


def scenario_table():
    return [_text("查询结果：\n\n"),
            _text("| # | 技能 | 版本 |\n|---|---|---|\n| 1 | ppt-translator | v1 |\n| 2 | skill-creator | v1 |\n"),
            _completed(total=5000)]


SCENARIOS = {"thinking_list": scenario_thinking_list, "long": scenario_long, "table": scenario_table,
             "unknown_reasoning": lambda: [
                 # 真正未知事件名带 delta（模拟 glm 走未识别的 reasoning 事件），应丢弃不显示
                 ("response.glm.reasoning", {"type": "response.glm.reasoning", "delta": "我是思考过程不该显示"}),
                 ("response.unknown.weird", {"delta": "怪事件内容该丢"}),
                 _text("正文该显示\n"),
                 _completed(total=5000),
             ]}


async def responses_handler(request):
    scenario = request.app["scenario"]
    events = SCENARIOS.get(scenario, scenario_thinking_list)()
    response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"})
    await response.prepare(request)
    await _send_events(response, events)
    return response


async def models_handler(request):
    return web.json_response({"data": [{"id": "glm-5.2", "context_window_tokens": 1000000}]})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    scenario = sys.argv[2] if len(sys.argv) > 2 else "thinking_list"
    app = web.Application()
    app["scenario"] = scenario
    app.router.add_post("/v1/responses", responses_handler)
    app.router.add_get("/v1/models", models_handler)
    print(f"mock responses server (aiohttp) on http://127.0.0.1:{port} scenario={scenario}", flush=True)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
