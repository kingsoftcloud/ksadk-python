"""Run an isolated real Studio for manual/browser Teams validation.

Set KSADK_TEAMS_LIVE_MODEL_KEY only in the process environment. The workspace
stores an env credential reference, never the key. This script does not submit
model work: create the group and send the goal from the real browser UI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_home import studio_dsh_home
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import AgentSpec
from ksadk.studio.service import StudioService


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8775)
    args = parser.parse_args()
    if not os.environ.get("KSADK_TEAMS_LIVE_MODEL_KEY"):
        raise RuntimeError("Set the model credential in the process environment")
    toolchain = DshToolchainManager()
    await asyncio.to_thread(toolchain.install)
    studio = StudioService(args.workspace)
    await studio.start()
    if not any(
        manifest.metadata.id == "io.ksadk.harness-provider"
        for manifest in studio._active_provider_manifests.values()
    ):

        def install():
            with DshProfilePluginBridge(
                dsh_home=studio_dsh_home(args.workspace),
                profile="web",
                dsh_command=toolchain.require_command(),
                cwd=args.workspace,
            ) as bridge:
                entry = bridge.install_plugin(
                    str(shipped_harness_dsh_bundle().root), accept_host_permissions=True
                )
                bridge.set_enabled(entry.name, enabled=True)

        await studio.reconfigure_dsh_profile(lambda: asyncio.to_thread(install))
    await studio.teams_installation.enable()
    if not studio.builds.list():
        spec = {
            "runtime": {"type": "harness"},
            "instructions": {
                "system": (
                    "遵守宿主提供的团队策略，简洁回答。评审示例只处理当前目标提供的信息。"
                    "成员完成任务时用 write_workspace_file 写一份简短 Markdown 报告，"
                    "team_publish_artifact 登记，再 team_submit_result 提交结果与 artifactIds。"
                    "如有 peer_check 工具，先委派它独立核对自己的结论。"
                    "Leader 只做分派和汇总，不承担成员任务；等待人工验收后再汇总。"
                )
            },
            "model": {
                "model": "deepseek-chat",
                "endpointUrl": "https://api.deepseek.com/chat/completions",
                "credentialRef": "env://KSADK_TEAMS_LIVE_MODEL_KEY",
                "parameters": {"maxTokens": 4096},
            },
            "capabilities": {
                "tools": [
                    {
                        "name": "write_workspace_file",
                        "version": "1.0.0",
                        "sideEffect": "write",
                        "approval": "always",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    }
                ]
            },
            "security": {
                "allowedPermissions": ["process:host-user"],
                "network": {"mode": "restricted", "allowedHosts": ["api.deepseek.com"]},
            },
        }
        if "sub_agents" in AgentSpec.model_fields:
            spec["subAgents"] = [
                {
                    "name": "peer_check",
                    "instructions": "独立检查给定结论，输出两条具体建议。",
                    "description": "委派临时评审助手独立核查自己的结论。",
                    "tools": [],
                    "max_turns": 2,
                    "max_total_tokens": 8000,
                }
            ]
        draft = studio.create_agent(
            agent_id="teams-live-reviewer",
            name="DeepSeek 协作评审",
            spec=AgentSpec.model_validate(spec),
        )
        operation = studio.submit_build(
            draft.metadata.id, revision=draft.metadata.revision, idempotency_key="live-build"
        )
        async def wait_for_build():
            while studio.operations.get(operation.id).status not in {"SUCCEEDED", "FAILED"}:
                await asyncio.sleep(0.1)

        await asyncio.wait_for(wait_for_build(), timeout=90)
        result = studio.operations.get(operation.id)
        if result.status != "SUCCEEDED":
            raise RuntimeError("Live validation Build failed")
    print(
        json.dumps(
            {
                "ready": True,
                "url": f"http://127.0.0.1:{args.port}/",
                "workspace": str(args.workspace),
                "buildIds": [item.id for item in studio.builds.list()],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    app = create_studio_app(args.workspace, service=studio)
    await uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    ).serve()


if __name__ == "__main__":
    asyncio.run(main())
