"""Run an outbound Teams execution node without an open Studio browser."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from ksadk.plugins.teams.errors import TeamsError


async def run_node(
    workspace: Path,
    server_url: str,
    *,
    kind: str,
    name: str | None = None,
    stop: asyncio.Event | None = None,
    service_factory=None,
    node_v1_factory=None,
):
    from ksadk.studio.service import StudioService

    settings = {"KSADK_TEAMS_SERVER_URL": server_url, "KSADK_TEAMS_NODE_KIND": kind}
    if name:
        settings["KSADK_TEAMS_NODE_NAME"] = name
    service = (service_factory or StudioService)(workspace, configuration_overrides=settings)
    if node_v1_factory is not None:
        service.teams_installation.node_v1_factory = node_v1_factory
    stopped = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    if stop is None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
                installed.append(signum)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        await service.teams_installation.enable()
        if service.teams_installation.node is None:
            raise TeamsError(
                "execution_node_unavailable", "节点未能启动，请检查固定构建与执行环境", status=503
            )
        await stopped.wait()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        await service.aclose()


def main():
    parser = argparse.ArgumentParser(
        description="运行 Agent Teams 出站执行节点；凭据从本地配置或环境读取"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--kind", choices=("local", "cloud"), default="local")
    parser.add_argument("--name")
    args = parser.parse_args()
    try:
        asyncio.run(run_node(args.workspace, args.server_url, kind=args.kind, name=args.name))
    except TeamsError as error:
        parser.exit(1, f"{error.code}: {error}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
