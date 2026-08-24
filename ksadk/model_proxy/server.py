"""app factory + 进程内转换 proxy 生命周期管理。

把 HTTP 层与配置/转换解耦:create_app(config) 返回 FastAPI app;
ProxyServer 在后台线程懒起一个 localhost uvicorn,幂等 + 加锁 + 干净回收(随 runner 退出)。
"""

import asyncio
import json
import logging
import socket
import threading
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import _LOOPBACK_HOSTS, ProxyConfig
from .transform import Streamer, UnsupportedToolsError, chat_to_response, responses_to_chat

logger = logging.getLogger(__name__)


def _request_id(response: httpx.Response) -> str:
    for name in (
        "x-request-id",
        "request-id",
        "x-ks-request-id",
        "x-amzn-requestid",
    ):
        value = response.headers.get(name)
        if value:
            return str(value)
    return ""


def _reported_usage(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return {
        "inputTokens": int(raw.get("prompt_tokens") or 0),
        "outputTokens": int(raw.get("completion_tokens") or 0),
        "totalTokens": int(raw.get("total_tokens") or 0),
        "cachedInputTokens": int(prompt_details.get("cached_tokens") or 0),
        "reasoningOutputTokens": int(completion_details.get("reasoning_tokens") or 0),
    }


def _emit_completed(
    config: ProxyConfig,
    *,
    response_id: str,
    model: object,
    status_code: int,
    started: float,
    usage: object = None,
) -> None:
    payload: dict[str, object] = {
        "responseId": response_id,
        "model": str(model or ""),
        "statusCode": status_code,
        "durationMs": max(0, int((time.monotonic() - started) * 1000)),
    }
    reported = _reported_usage(usage)
    if reported is not None:
        payload["usage"] = reported
    config.emit("proxy.completed", payload)


def _upstream_error(status_code: int) -> JSONResponse:
    """Return a stable public error without relaying an upstream traceback."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "type": "upstream_error",
                "message": "The model upstream rejected the request.",
            }
        },
    )


def create_app(config: ProxyConfig) -> FastAPI:
    base = config.upstream_base.rstrip("/")
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    app = FastAPI()
    # 应用层取消信号:stop() 时置位,让活动 SSE 的 _stream_gen 主动 break(比单靠 force_exit 可靠)
    cancel = threading.Event()
    app.state.cancel_event = cancel

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "upstream": base}

    @app.post("/v1/chat/completions")
    async def chat_passthrough(req: Request):
        """chat 直通端点(env 框架发 chat 经代理原样转发上游,不转换)。

        让代理成为统一模型出口:env 框架(ADK/langchain/deepagents 发 chat)经此直通,
        codex(发 responses)经 /v1/responses 转换。统一出口为后续能力探测/路由打底。
        chat 直通字节级转发 + 流式透传,前缀缓存不受影响。
        """
        if (
            config.local_token
            and req.headers.get("authorization", "") != f"Bearer {config.local_token}"
        ):
            return JSONResponse(
                status_code=401, content={"error": {"type": "authentication_error"}}
            )
        body = await req.json()
        is_stream = bool(body.get("stream"))
        up_headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        if is_stream:
            return StreamingResponse(
                _chat_passthrough_stream(config, base, up_headers, body),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        async with httpx.AsyncClient(timeout=config.timeout) as c:
            r = await c.post(f"{base}/chat/completions", json=body, headers=up_headers)
            if r.is_error:
                logger.warning("chat upstream rejected request: status=%s", r.status_code)
                return _upstream_error(r.status_code)
            return JSONResponse(status_code=r.status_code, content=r.json() if r.content else None)

    @app.post("/v1/responses")
    async def responses(req: Request):
        # 本地鉴权:codex -> 本地 proxy 用 KSADK_PROXY_TOKEN;上游凭证独立保存于 config.api_key
        if (
            config.local_token
            and req.headers.get("authorization", "") != f"Bearer {config.local_token}"
        ):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {"type": "authentication_error", "message": "invalid proxy token"}
                },
            )
        body = await req.json()
        try:
            chat_req, restore_map = responses_to_chat(body)
        except UnsupportedToolsError as e:
            logger.info("responses request uses unsupported tools: %s", e)
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "unsupported_tools",
                        "message": f"The request uses tools unsupported by the model upstream: {e}",
                    }
                },
            )
        # codex 会对内建能力发内部伪模型名(如 auto_review guardian 用 codex-auto-review),
        # 单上游代理必须落回配置的真实模型,否则上游按未知模型 403。
        if config.upstream_model and chat_req.get("model") != config.upstream_model:
            logger.debug(
                "responses model rewrite: %s -> %s",
                chat_req.get("model"),
                config.upstream_model,
            )
            chat_req["model"] = config.upstream_model
        rid = "resp_" + uuid.uuid4().hex[:24]
        model = body.get("model")
        started = time.monotonic()
        config.emit(
            "proxy.requested",
            {
                "responseId": rid,
                "model": str(model or ""),
                "protocol": "responses-to-chat",
                "stream": bool(body.get("stream")),
            },
        )
        if body.get("stream"):
            chat_req["stream"] = True
            chat_req["stream_options"] = {"include_usage": True}
            return StreamingResponse(
                _stream_gen(
                    config,
                    base,
                    headers,
                    chat_req,
                    rid,
                    model,
                    cancel,
                    restore_map,
                    started,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        async with httpx.AsyncClient(timeout=config.timeout) as c:
            r = await c.post(f"{base}/chat/completions", json=chat_req, headers=headers)
            config.emit(
                "proxy.upstream",
                {"requestId": _request_id(r), "statusCode": r.status_code},
            )
            if r.status_code != 200:
                _emit_completed(
                    config,
                    response_id=rid,
                    model=model,
                    status_code=r.status_code,
                    started=started,
                )
                logger.warning("responses upstream rejected request: status=%s", r.status_code)
                return _upstream_error(r.status_code)
            upstream_payload = r.json()
            _emit_completed(
                config,
                response_id=rid,
                model=model,
                status_code=r.status_code,
                started=started,
                usage=upstream_payload.get("usage"),
            )
            return JSONResponse(chat_to_response(upstream_payload, rid, restore_map))

    return app


async def _chat_passthrough_stream(config: ProxyConfig, base: str, headers: dict, body: dict):
    """chat SSE 字节级透传(不转换),让前缀缓存/流式格式与直连一致。"""
    try:
        async with httpx.AsyncClient(timeout=config.timeout) as c:
            async with c.stream(
                "POST", f"{base}/chat/completions", json=body, headers=headers
            ) as r:
                async for line in r.aiter_lines():
                    yield line + "\n"
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat upstream stream failed", exc_info=exc)
        yield 'data: {"error":"The model upstream stream failed."}\n\n'


async def _stream_gen(
    config: ProxyConfig,
    base: str,
    headers: dict,
    chat_req: dict,
    rid: str,
    model,
    cancel: threading.Event,
    restore_map: dict | None = None,
    started: float | None = None,
):
    started = time.monotonic() if started is None else started
    s = Streamer(rid, model, restore_map)
    for e in s.start():
        yield e
    try:
        # 超时必须有界(config.timeout):无限超时的活动 SSE 会让 stop() 后线程仍存活
        async with httpx.AsyncClient(timeout=config.timeout) as c:
            async with c.stream(
                "POST", f"{base}/chat/completions", json=chat_req, headers=headers
            ) as r:
                config.emit(
                    "proxy.upstream",
                    {"requestId": _request_id(r), "statusCode": r.status_code},
                )
                if r.status_code != 200:
                    _emit_completed(
                        config,
                        response_id=rid,
                        model=model,
                        status_code=r.status_code,
                        started=started,
                    )
                    logger.warning(
                        "responses upstream stream rejected: status=%s model=%s tools=%s "
                        "msgs=%s bytes=%s has_text_format=%s",
                        r.status_code,
                        chat_req.get("model"),
                        len(chat_req.get("tools") or []),
                        len(chat_req.get("messages") or []),
                        len(json.dumps(chat_req)),
                        bool(chat_req.get("response_format")),
                    )
                    yield Streamer.ev(
                        "response.failed",
                        {
                            "type": "response.failed",
                            "response": {
                                **s._resp("failed"),
                                "error": {"message": "The model upstream rejected the request."},
                            },
                        },
                    )
                    return
                async for line in r.aiter_lines():
                    if cancel.is_set():
                        _emit_completed(
                            config,
                            response_id=rid,
                            model=model,
                            status_code=499,
                            started=started,
                            usage=s.usage,
                        )
                        return  # stop() 置位:主动中断活动 SSE,让线程能及时回收
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    for e in s.handle(chunk):
                        yield e
    except Exception as exc:  # noqa: BLE001
        _emit_completed(
            config,
            response_id=rid,
            model=model,
            status_code=502,
            started=started,
            usage=s.usage,
        )
        logger.warning("responses upstream stream failed", exc_info=exc)
        yield Streamer.ev(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    **s._resp("failed"),
                    "error": {"message": "The model upstream stream failed."},
                },
            },
        )
        return
    for e in s.finalize():
        yield e
    _emit_completed(
        config,
        response_id=rid,
        model=model,
        status_code=200,
        started=started,
        usage=s.usage,
    )


class ProxyServer:
    """进程内 localhost 转换 proxy 的生命周期管理。

    - start 幂等(已启动直接返回)+ 加锁,避免重复 start 覆盖 server/thread 造成泄漏;
    - 端口在 start 时 bind 并**一直持有 socket**(传给 uvicorn),避免"先释放再重绑"的 TOCTOU;
    - 启动超时与 stop 都会干净回收线程与 socket。
    """

    def __init__(self, config: ProxyConfig, host: str = "127.0.0.1", port: int = 0):
        if host not in _LOOPBACK_HOSTS and not config.local_token:
            raise ValueError(
                f"非回环监听(host={host})必须设置 local_token,否则是无鉴权代持上游凭证的开放代理"
            )
        self.config = config
        self.host = host
        self._requested_port = port
        self.port = port
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._app: FastAPI | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self, wait_ready: float = 10.0) -> str:
        with self._lock:
            if self._server is not None:
                return self.base_url  # 幂等
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, self._requested_port))
            except OSError:
                sock.close()
                raise
            self.port = int(sock.getsockname()[1])
            app = create_app(self.config)
            self._app = app
            # timeout_graceful_shutdown 设上限:stop 时挂在 yield 的 SSE generator 不再消费,
            # 若无限等待连接完成(None 默认)会让 shutdown 卡死、线程回收不掉
            cfg = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=1)
            self._sock = sock
            server = uvicorn.Server(cfg)
            self._server = server
            # serve(sockets=[sock]) 跨平台:uvicorn 直接复用已绑定 socket,
            # 不走 Config(fd=...) 那条仅 Unix 可用的 socket.fromfd 路径
            self._thread = threading.Thread(
                target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True
            )
            self._thread.start()
            deadline = time.time() + wait_ready
            while time.time() < deadline:
                try:
                    if (
                        httpx.get(f"http://{self.host}:{self.port}/healthz", timeout=1).status_code
                        == 200
                    ):
                        return self.base_url
                except Exception:  # noqa: BLE001
                    time.sleep(0.05)
            self._teardown()  # 启动超时也要清理线程与 socket
            raise RuntimeError(f"ProxyServer 未在 {wait_ready}s 内就绪")

    def stop(self):
        with self._lock:
            self._teardown()

    def _teardown(self):
        if self._app is not None:
            # 应用层取消信号:让活动 SSE 的 _stream_gen 主动 break
            self._app.state.cancel_event.set()
        if self._server is not None:
            # should_exit 触发主循环进入 shutdown;force_exit 让 shutdown 跳过等待
            # connections/tasks 完成(活动 SSE 下不再无限等)。两者缺一:只 should_exit
            # 会被活动连接卡死,只 force_exit 主循环根本不进 shutdown。
            self._server.should_exit = True
            self._server.force_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._server = None
        self._thread = None
        self._sock = None
        self._app = None
