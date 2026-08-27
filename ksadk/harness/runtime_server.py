"""Harness Runtime 独立进程入口（§12.4 收口 4）。

Local Deployment 的进程形态：以 uvicorn 起真实 HTTP 服务，暴露
``/health``（供 LocalLifecycleManager 做真实健康检查）与 ``/manifest``
（Build Manifest 投影）。对话数据面仍由宿主进程（Studio）经 Active
Route 驱动；本进程是 Runtime 的"活着"事实——端口、PID、健康状态。

用法::

    python -m ksadk.harness.runtime_server \\
        --spec-file /tmp/spec.json --route studio://p/local \\
        --deployment-id dep_x --port 8123
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI


def build_deployment_app(
    *,
    deployment_id: str,
    route: str,
    spec_payload: dict[str, Any],
    build_id: str = "",
    content_hash: str = "",
) -> FastAPI:
    """构建单 Deployment 的 Runtime 服务（Health + Manifest）。"""

    app = FastAPI(title="KsADK Harness Runtime", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "deploymentId": deployment_id,
            "route": route,
            "buildId": build_id,
        }

    @app.get("/manifest")
    async def manifest() -> dict[str, Any]:
        return {
            "deploymentId": deployment_id,
            "route": route,
            "buildId": build_id,
            "contentHash": content_hash,
            "agentRevisionRef": spec_payload.get("agentRevisionRef", ""),
            "modelProfileRef": (spec_payload.get("model") or {}).get("profileRef", ""),
        }

    @app.post("/runs")
    async def runs() -> dict[str, Any]:
        # 数据面未接入（本地 MVP）：Health/Route 事实已建立，正式对话仍由
        # 宿主进程经 Active Route 驱动。诚实声明而非假装可调用。
        return {
            "error": "NOT_IMPLEMENTED",
            "message": (
                "本地 Runtime 进程当前只承载 Health/Route 事实；"
                "对话数据面经宿主 Studio 的 Active Route 执行"
            ),
        }

    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ksadk.harness.runtime_server")
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--build-id", default="")
    parser.add_argument("--content-hash", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    spec_payload = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    app = build_deployment_app(
        deployment_id=args.deployment_id,
        route=args.route,
        spec_payload=spec_payload,
        build_id=args.build_id,
        content_hash=args.content_hash,
    )
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
