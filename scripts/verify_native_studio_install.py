"""Exercise ordinary default Studio assembly without importing agent frameworks.

Uses the shipped DSH provider and real file tools. Model HTTP and DSH discovery
are controlled fixtures; this checks installation/assembly, not model quality.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.abc
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

DENIED = {"langgraph", "langchain", "langchain_core", "langchain_openai", "deepagents", "sqlalchemy"}


class NoFrameworks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname.split(".")[0] in DENIED:
            raise ModuleNotFoundError("framework forbidden in Native smoke", name=fullname)


class AllowFixtureNetwork:
    async def check(self, _endpoint, _policy):  # noqa: ANN001
        return None


async def exercise(directory: Path, *, streaming: bool, durable: bool | None) -> dict:
    import httpx

    from ksadk.model_gateway.client import ModelGatewayClient
    from ksadk.plugins.dsh_home import prepare_studio_dsh_home
    from ksadk.plugins.dsh_toolchain import DSH_VERSION
    from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
    from ksadk.studio.contracts import (
        AgentBindings,
        AgentSpec,
        CapabilityBinding,
        Instructions,
        ModelSpec,
        NetworkPolicy,
        RunStatus,
        RuntimeRef,
        SecuritySpec,
    )
    from ksadk.studio.service import StudioService

    # Install the shipped provider into an isolated, correctly versioned profile.
    home = directory / "dsh-home"
    prepare_studio_dsh_home(home)
    profile = home / "profiles/studio"
    installed = profile / "node_modules/@kingsoftcloud/ksadk-harness-provider"
    installed.parent.mkdir(parents=True)
    shutil.copytree(shipped_harness_dsh_bundle().root, installed)
    (profile / "package.json").write_text(json.dumps({
        "dependencies": {"@kingsoftcloud/ksadk-harness-provider": "1.0.0"},
        "dsh": {"profile": {"bundles": ["@kingsoftcloud/ksadk-harness-provider"]}},
    }))
    executable = directory / "dsh-discovery-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *--version*) echo {DSH_VERSION};;\n"
        "  *--dump-config*) echo 'profile: studio; harness: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
    )
    executable.chmod(0o700)
    os.environ.update({
        "KSADK_DSH_HOME": str(home), "KSADK_DSH_PROFILE": "studio",
        "KSADK_DSH_BIN": str(executable), "KSADK_SANDBOX_BACKEND": "local",
        "NATIVE_INSTALL_FIXTURE_KEY": "fixture-key",
    })
    for name in ("KSADK_STUDIO_NATIVE_AGENT_IDS", "KSADK_STUDIO_NATIVE_DEFAULT",
                 "KSADK_STUDIO_NATIVE_DURABLE"):
        os.environ.pop(name, None)
    requests = []

    def handler(request):  # noqa: ANN001
        body = json.loads(request.content)
        requests.append(body)
        assert bool(body.get("stream")) is streaming
        seen = [item for item in body["messages"] if item.get("role") == "tool"]
        message = {"role": "assistant", "content": "NATIVE_INSTALL_OBSERVED"}
        finish = "stop"
        if not seen:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "install-read", "type": "function", "function": {
                    "name": "read_workspace_file",
                    "arguments": json.dumps({"path": "proof.txt"}),
                },
            }]}
            finish = "tool_calls"
        else:
            assert "NATIVE_INSTALL_FILE" in str(seen[-1]["content"])
        usage = {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
        if not streaming:
            return httpx.Response(200, json={
                "id": "fixture", "choices": [{"index": 0, "message": message,
                                                 "finish_reason": finish}], "usage": usage,
            })
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
        chunks = [
            {"id": "fixture", "choices": [{"index": 0, "delta": delta,
                                             "finish_reason": None}]},
            {"id": "fixture", "choices": [{"index": 0, "delta": {},
                                             "finish_reason": finish}], "usage": usage},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"data: {json.dumps(x)}\n\n" for x in chunks)
                              + "data: [DONE]\n\n")

    class Gateway(ModelGatewayClient):
        def supports_streaming(self, _model, *, requested=True):  # noqa: ANN001
            return streaming

    gateway = Gateway(network_guard=AllowFixtureNetwork(), transport=httpx.MockTransport(handler))
    root = directory / "workspace"
    tool_workspace = root / ".harness-tools/workspace"
    tool_workspace.mkdir(parents=True)
    (tool_workspace / "proof.txt").write_text("NATIVE_INSTALL_FILE")
    # No native Agent IDs, custom selector, adapter or reasoner are injected.
    storage = {} if durable is None else {"native_harness_durable": durable}
    expected_durable = True if durable is None else durable
    studio = StudioService(root, model_client=gateway, **storage)
    try:
        entry = next(item for item in studio.runtime_catalog() if item["runtimeType"] == "harness")
        assert entry["adapter"] == "NativeRuntimeAdapter"
        assert entry["capabilities"]["durableAcrossProcess"] is expected_durable
        assert entry["capabilities"]["checkpoint"] == "not_supported"
        tool = next(item for item in studio.catalog.list(kind="tool", limit=200)
                    if item.name == "read_workspace_file")
        for agent in ("default-native-one", "default-native-two"):
            studio.create_agent(agent_id=agent, name=agent, spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Read proof.txt and report its marker."),
                model=ModelSpec(model="fixture-model", endpoint_url="https://example.test/model",
                                credential_ref="env://NATIVE_INSTALL_FIXTURE_KEY"),
                bindings=AgentBindings(policy_template="loose", tools=[
                    CapabilityBinding(resource_id=tool.resource_id),
                ]),
                security=SecuritySpec(allowed_permissions=["process:host-user"],
                                      network=NetworkPolicy(allowed_hosts=["example.test"])),
            ))
            build = await studio.ensure_current_build(agent)
            first = await studio.run_build(build.id, "Read proof.txt.", agent)
            assert first.status == RunStatus.COMPLETED, first.error
            assert first.runtime_handle["native_ref"]["engine_kind"] == "native"
            capabilities = studio.conversation_surface(build.id, session_id=agent)
            assert any(item.name == "attachment.file" for item in capabilities.inputs)
            second = await studio.run_build(build.id, "Repeat the observed marker.", agent)
            assert second.status == RunStatus.COMPLETED, second.status
            assert second.runtime_handle["native_ref"]["engine_kind"] == "native"
        assert len(requests) == 6
        assert (tool_workspace / "proof.txt").read_text() == "NATIVE_INSTALL_FILE"
        assert not any(name.split(".")[0] in DENIED for name in sys.modules)
        return {"agents": 2, "runs": 4, "model_http": len(requests),
                "streaming": streaming, "durable": expected_durable, "engine": "native",
                "storage_selection": "ordinary_default" if durable is None else "explicit",
                "loaded_frameworks": [], "real_model_quality": False,
                "dsh_discovery": "fixture", "tool_execution": "actual-sdk-worker"}
    finally:
        await studio.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--streaming", action="store_true")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--durable", action="store_const", const=True, dest="durable")
    storage.add_argument("--memory", action="store_const", const=False, dest="durable")
    parser.set_defaults(durable=None)
    parser.add_argument("--require-clean-install", action="store_true")
    args = parser.parse_args()
    sys.meta_path.insert(0, NoFrameworks())
    distributions = sorted(d.metadata["Name"] for d in importlib.metadata.distributions())
    framework_packages = [name for name in distributions
                          if name.lower().replace("_", "-").startswith(
                              ("langgraph", "langchain", "deepagents"))]
    if args.require_clean_install:
        assert not framework_packages, framework_packages
    with tempfile.TemporaryDirectory(prefix="ksadk-native-install-") as temporary:
        result = asyncio.run(exercise(Path(temporary), streaming=args.streaming,
                                      durable=args.durable))
    result["installed_frameworks"] = framework_packages
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
