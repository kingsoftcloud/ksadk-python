from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from ksadk.studio.api import create_studio_app
from ksadk.studio.codex_authoring import (
    CodexAuthoringExecutor,
    CodexAuthoringUnavailableError,
)
from ksadk.studio.contracts import MCPServerRef, ModelSpec, Usage
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService

_VALID_PATCH = {
    "name": "代码评审助手",
    "slug": "code-review-helper",
    "runtimeType": "codex",
    "description": "审查代码并给出证据",
    "spec": {
        "instructions": {
            "system": "你是代码评审助手，输出阻断项和证据。",
            "task": "审查给定 diff。",
        }
    },
}

_INVALID_PATCH = "name: 只有名字"


_PROFILE_ID = {"value": ""}


def _model_profile_id() -> str:
    return _PROFILE_ID["value"] or "model/deepseek-v4-pro"


def _register_model(studio: StudioService) -> None:
    profile = studio.catalog.create_model_profile(
        name="deepseek-v4-pro",
        display_name="DeepSeek v4 Pro",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            provider="openai-compatible",
            model="deepseek-v4-pro",
            endpoint_url="https://example.com/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )
    _PROFILE_ID["value"] = profile.resource_id


class _FakeCodexClient:
    """按脚本演出的 fake codex：每轮按计划写（或不写）manifest 文件。"""

    def __init__(self, script: list[str | None], *, cwd_key: str = "cwd") -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.thread_config: dict | None = None
        self.env: dict[str, str] | None = None
        self.closed = False
        self.cwd_key = cwd_key
        self.manifest_name = "agentkit.yaml"

    async def start_thread(self, config=None) -> str:
        self.thread_config = dict(config or {})
        return "thread-1"

    async def run_turn(self, _thread_id: str, prompt: str, *, config=None):
        self.prompts.append(prompt)
        content = self.script.pop(0) if self.script else None
        if content is not None and self.thread_config:
            manifest_dir = Path(str(self.thread_config[self.cwd_key]))
            manifest_dir.mkdir(parents=True, exist_ok=True)
            (manifest_dir / self.manifest_name).write_text(content, encoding="utf-8")
        yield {
            "method": "item/agentMessage/delta",
            "params": {"delta": "已写入文件"},
        }
        yield {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "totalTokens": 120,
                    }
                },
            },
        }

    async def close(self) -> None:
        self.closed = True


class _StaticCredentials:
    def resolve(self, _reference: str) -> str:
        return "test-key"


def _executor(
    tmp_path: Path, script: list[str | None], **kwargs
) -> tuple[CodexAuthoringExecutor, _FakeCodexClient]:
    studio = StudioService(tmp_path)
    client = _FakeCodexClient(script)
    executor = CodexAuthoringExecutor(
        studio.workspace,
        _StaticCredentials(),
        client_factory=lambda env: client,
        **kwargs,
    )
    executor._fake_client = client  # type: ignore[attr-defined]
    return executor, client


def _resolved_model(studio: StudioService):
    from ksadk.studio.contracts import AgentBindings

    spec = studio.catalog.resolve_model(AgentBindings(model_profile_id=_model_profile_id()))
    assert spec is not None
    return studio.catalog.resolver.resolve_model(spec)


def _messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "帮我做一个代码评审 agent"}]


async def test_codex_authoring_converges_after_correction(tmp_path: Path) -> None:
    executor, client = _executor(
        tmp_path, [_INVALID_PATCH, json.dumps(_VALID_PATCH, ensure_ascii=False)]
    )
    result = await executor.compose(
        messages=_messages(),
        model=_fake_model(),
        request_id="req-abc",
    )
    assert result.attempts == 2
    assert result.proposal.slug == "code-review-helper"
    assert "未通过" in client.prompts[1]
    assert result.usage.input_tokens == 200  # 两个 turn 各 100
    assert result.usage.source == "codex-authoring"
    # 成功后清理请求目录
    assert not (tmp_path / ".agentkit/authoring/codex-req-req-abc").exists()
    assert client.closed


def _fake_model():
    from ksadk.studio.contracts import ResolvedModel

    return ResolvedModel(
        provider="openai-compatible",
        model="deepseek-v4-pro",
        endpoint_url="https://example.com/v1/chat/completions",
        credential_ref="env://AGENTKIT_MODEL_API_KEY",
        parameters={},
    )


async def test_codex_authoring_first_try_success(tmp_path: Path) -> None:
    executor, client = _executor(tmp_path, [json.dumps(_VALID_PATCH, ensure_ascii=False)])
    result = await executor.compose(messages=_messages(), model=_fake_model(), request_id="req-ok")
    assert result.attempts == 1
    assert result.proposal.name == "代码评审助手"
    assert len(client.prompts) == 1
    # builder prompt 带工作区文件路径与对话内容
    assert "agentkit.yaml" in client.prompts[0]
    assert "代码评审 agent" in client.prompts[0]
    # codex env 注入模型连接信息（端点去掉 chat/completions 后缀）
    assert client.thread_config is not None
    assert client.thread_config["sandbox"] == "workspace-write"
    assert client.thread_config["model"] == "deepseek-v4-pro"


async def test_codex_authoring_exhausts_retries(tmp_path: Path) -> None:
    executor, client = _executor(
        tmp_path,
        [None, _INVALID_PATCH, "name: 仍不合法"],
        max_retries=2,
    )
    with pytest.raises(StudioError) as excinfo:
        await executor.compose(messages=_messages(), model=_fake_model(), request_id="req-bad")
    assert excinfo.value.code == "AUTHORING_MODEL_OUTPUT_INVALID"
    assert excinfo.value.details["attemptedCorrections"] == 2
    # 失败目录保留供诊断
    failed_dir = tmp_path / ".agentkit/authoring/codex-req-req-bad"
    assert failed_dir.is_dir()
    assert client.closed


async def test_codex_authoring_prunes_failed_directories(tmp_path: Path) -> None:
    executor, _client = _executor(tmp_path, [None], max_retries=0)
    authoring_root = tmp_path / ".agentkit/authoring"
    # 显式递增 mtime:紧循环 mkdir 在快文件系统上时间戳并列,会让按 st_mtime
    # 排序的 prune 删错目录(CI 上曾误删 old-2/old-5 而非最旧的 old-0/old-1)。
    import os as _os

    for index in range(6):
        d = authoring_root / f"codex-req-old-{index}"
        d.mkdir(parents=True, exist_ok=True)
        _os.utime(d, (1_000_000 + index, 1_000_000 + index))
    with pytest.raises(StudioError):
        await executor.compose(messages=_messages(), model=_fake_model(), request_id="req-new")
    remaining = sorted(path.name for path in authoring_root.glob("codex-req-*") if path.is_dir())
    assert len(remaining) == 5
    assert "codex-req-old-0" not in remaining
    assert "codex-req-req-new" in remaining


async def test_codex_authoring_timeout_falls_unavailable(tmp_path: Path) -> None:
    class _HangingClient(_FakeCodexClient):
        async def run_turn(self, _thread_id, prompt, *, config=None):
            self.prompts.append(prompt)
            import asyncio

            await asyncio.sleep(10)
            yield {"method": "item/agentMessage/delta", "params": {"delta": "x"}}

    client = _HangingClient([None])
    studio = StudioService(tmp_path)
    executor = CodexAuthoringExecutor(
        studio.workspace,
        _StaticCredentials(),
        client_factory=lambda env: client,
        turn_timeout_seconds=0.05,
    )
    with pytest.raises(CodexAuthoringUnavailableError):
        await executor.compose(messages=_messages(), model=_fake_model(), request_id="req-timeout")
    assert client.closed


def test_probe_missing_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys

    studio = StudioService(tmp_path)
    executor = CodexAuthoringExecutor(studio.workspace, _StaticCredentials())
    # sys.modules 条目为 None 时 ``import openai_codex`` 直接 ImportError。
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    with pytest.raises(CodexAuthoringUnavailableError):
        executor.probe()


class _FakeExecutor:
    """Coordinator 层测试用的可脚本化执行器。"""

    def __init__(
        self,
        *,
        script: list[str | None] | None = None,
        unavailable: Exception | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.script = list(script or [])
        self.unavailable = unavailable
        self.probe_error = probe_error
        self.calls: list[dict] = []
        self.probes = 0

    def probe(self) -> None:
        self.probes += 1
        if self.probe_error is not None:
            raise self.probe_error

    async def compose(self, *, messages, model, base, request_id):
        self.calls.append(
            {"messages": messages, "model": model, "base": base, "request_id": request_id}
        )
        if self.unavailable is not None:
            raise self.unavailable
        content = self.script.pop(0) if self.script else None
        if content is None:
            raise StudioError("AUTHORING_MODEL_OUTPUT_INVALID", "exhausted", 502)
        from ksadk.studio.authoring import AgentAuthoringService

        proposal = AgentAuthoringService.parse_conversation_proposal(content, base=base)
        from ksadk.studio.codex_authoring import CodexAuthoringResult

        return CodexAuthoringResult(
            proposal=proposal,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            attempts=1,
            final_message="done",
            request_id=str(request_id or "req"),
        )


class _AuthoringModelClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0
        self.models: list[str] = []

    async def complete(self, model, *, messages, **kwargs):
        self.calls += 1
        self.models.append(str(getattr(model, "model", "")))
        from ksadk.studio.model_client import ModelResponse

        return ModelResponse(
            content=self.content,
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=10, total_tokens=20),
            tool_calls=[],
            raw_message={"role": "assistant", "content": self.content},
        )


def _install(studio: StudioService, executor) -> None:
    studio.authoring.codex_authoring = executor


async def test_coordinator_uses_codex_when_configured(tmp_path: Path) -> None:
    valid = json.dumps(_VALID_PATCH, ensure_ascii=False)
    model_client = _AuthoringModelClient(valid)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    executor = _FakeExecutor(script=[valid])
    _install(studio, executor)
    import os

    old = os.environ.get("KSADK_STUDIO_AUTHORIZER")
    os.environ["KSADK_STUDIO_AUTHORIZER"] = "codex"
    try:
        result = await studio.compose_agent_conversation(
            messages=_messages(),
            model_profile_id=_model_profile_id(),
            request_id="coord-1",
        )
    finally:
        if old is None:
            os.environ.pop("KSADK_STUDIO_AUTHORIZER", None)
        else:
            os.environ["KSADK_STUDIO_AUTHORIZER"] = old
    assert result["authoringMode"] == "codex"
    assert result["proposal"]["slug"] == "code-review-helper"
    assert model_client.calls == 0
    assert len(executor.calls) == 1
    status = studio.conversation_authoring_status("coord-1")
    assert status["stage"] == "done"


async def test_coordinator_falls_back_to_chat_on_unavailable(
    tmp_path: Path,
) -> None:
    valid = json.dumps(_VALID_PATCH, ensure_ascii=False)
    model_client = _AuthoringModelClient(valid)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    _install(
        studio,
        _FakeExecutor(unavailable=CodexAuthoringUnavailableError("codex 探测失败")),
    )
    import os

    old = os.environ.get("KSADK_STUDIO_AUTHORIZER")
    os.environ["KSADK_STUDIO_AUTHORIZER"] = "codex"
    try:
        result = await studio.compose_agent_conversation(
            messages=_messages(),
            model_profile_id=_model_profile_id(),
            request_id="coord-2",
        )
    finally:
        if old is None:
            os.environ.pop("KSADK_STUDIO_AUTHORIZER", None)
        else:
            os.environ["KSADK_STUDIO_AUTHORIZER"] = old
    assert result["authoringMode"] == "chat"
    assert model_client.calls == 1  # chat 链首次即解析成功
    assert result["proposal"]["slug"] == "code-review-helper"


async def test_coordinator_probe_failure_selects_chat(tmp_path: Path) -> None:
    valid = json.dumps(_VALID_PATCH, ensure_ascii=False)
    model_client = _AuthoringModelClient(valid)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    executor = _FakeExecutor(probe_error=CodexAuthoringUnavailableError("no sdk"))
    _install(studio, executor)
    import os

    old = os.environ.pop("KSADK_STUDIO_AUTHORIZER", None)
    try:
        result = await studio.compose_agent_conversation(
            messages=_messages(),
            model_profile_id=_model_profile_id(),
            request_id="coord-3",
        )
    finally:
        if old is not None:
            os.environ["KSADK_STUDIO_AUTHORIZER"] = old
    assert result["authoringMode"] == "chat"
    assert executor.calls == []


async def test_coordinator_explicit_chat_mode_skips_codex(tmp_path: Path) -> None:
    valid = json.dumps(_VALID_PATCH, ensure_ascii=False)
    model_client = _AuthoringModelClient(valid)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    executor = _FakeExecutor(script=[valid])
    _install(studio, executor)
    import os

    old = os.environ.get("KSADK_STUDIO_AUTHORIZER")
    os.environ["KSADK_STUDIO_AUTHORIZER"] = "chat"
    try:
        result = await studio.compose_agent_conversation(
            messages=_messages(),
            model_profile_id=_model_profile_id(),
            request_id="coord-4",
        )
    finally:
        if old is None:
            os.environ.pop("KSADK_STUDIO_AUTHORIZER", None)
        else:
            os.environ["KSADK_STUDIO_AUTHORIZER"] = old
    assert result["authoringMode"] == "chat"
    assert executor.calls == []


async def test_coordinator_defaults_to_lightweight_chat_and_injects_selected_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通对话创建不启动 Codex，并且模型只能来自所选 Profile。"""

    model_patch = {
        **_VALID_PATCH,
        "spec": {
            **_VALID_PATCH["spec"],
            "model": {
                "model": "hallucinated-model",
                "baseUrl": "https://invalid.example/v1",
                "credentialRef": "env://NOT_A_REAL_PROFILE",
            },
            "bindings": {
                "modelProfileId": "model:provider:hallucinated:live",
                "modelProfileIds": ["model:provider:hallucinated:live"],
            },
        },
    }
    model_client = _AuthoringModelClient(json.dumps(model_patch, ensure_ascii=False))
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    executor = _FakeExecutor(script=[json.dumps(_VALID_PATCH, ensure_ascii=False)])
    _install(studio, executor)
    monkeypatch.delenv("KSADK_STUDIO_AUTHORIZER", raising=False)

    result = await studio.compose_agent_conversation(
        messages=_messages(),
        model_profile_id=_model_profile_id(),
        request_id="coord-default-chat",
    )

    assert result["authoringMode"] == "chat"
    assert model_client.calls == 1
    assert executor.probes == 0
    assert executor.calls == []
    spec = result["proposal"]["spec"]
    assert spec["bindings"]["modelProfileId"] == _model_profile_id()
    assert spec["bindings"]["modelProfileIds"] == [_model_profile_id()]
    assert spec["model"] is None


async def test_coordinator_separates_single_authoring_model_from_agent_model_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model that writes a Draft Patch is never the Agent model policy."""

    model_client = _AuthoringModelClient(json.dumps(_VALID_PATCH, ensure_ascii=False))
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    agent_fallback = studio.catalog.create_model_profile(
        name="glm-5.3",
        display_name="GLM 5.3",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            provider="openai-compatible",
            model="glm-5.3",
            endpoint_url="https://models.example.test/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )
    monkeypatch.delenv("KSADK_STUDIO_AUTHORIZER", raising=False)

    result = await studio.compose_agent_conversation(
        messages=_messages(),
        # The selected profile is used only for authoring this request.
        model_profile_id=_model_profile_id(),
        # The resulting Agent can use two models and picks the first supplied
        # (the UI sends its newest-first selection) as the runtime default.
        agent_model_profile_ids=[agent_fallback.resource_id, _model_profile_id()],
        agent_default_model_profile_id=agent_fallback.resource_id,
    )

    spec = result["proposal"]["spec"]
    assert model_client.models == ["deepseek-v4-pro"]
    assert spec["model"] is None
    assert spec["bindings"]["modelProfileId"] == agent_fallback.resource_id
    assert spec["bindings"]["modelProfileIds"] == [
        agent_fallback.resource_id,
        _model_profile_id(),
    ]


async def test_coordinator_injects_only_selected_capability_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP/Skill/Tool ids are selected and verified by Studio, never the LLM."""

    generic_patch = {**_VALID_PATCH, "runtimeType": "langgraph"}
    model_client = _AuthoringModelClient(
        json.dumps(
            {
                **generic_patch,
                "spec": {
                    **generic_patch["spec"],
                    "bindings": {
                        "tools": [{"resourceId": "tool:fake:invented:1.0.0"}],
                        "mcpServers": [{"resourceId": "mcp:fake:invented:1.0.0"}],
                        "skills": [{"resourceId": "skill:fake:invented:1.0.0"}],
                    },
                },
            },
            ensure_ascii=False,
        )
    )
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    mcp = studio.catalog.create_mcp_server(
        display_name="Review MCP",
        description="",
        server=MCPServerRef(
            name="review-mcp",
            version="1.0.0",
            transport="http",
            endpoint_url="https://mcp.example.test/rpc",
        ),
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "review/SKILL.md",
            "---\nname: Review Skill\ndescription: Review carefully\n"
            "version: 1.0.0\n---\nReview.\n",
        )
    skill = studio.catalog.import_skill_zip(archive.getvalue(), filename="review.zip")
    tool = studio.catalog.list(kind="tool", limit=1)[0]
    monkeypatch.delenv("KSADK_STUDIO_AUTHORIZER", raising=False)

    result = await studio.compose_agent_conversation(
        messages=_messages(),
        model_profile_id=_model_profile_id(),
        runtime_type="langgraph",
        tool_resource_ids=[tool.resource_id],
        mcp_resource_ids=[mcp.resource_id],
        skill_resource_ids=[skill.resource_id],
    )

    bindings = result["proposal"]["spec"]["bindings"]
    assert [item["resourceId"] for item in bindings["tools"]] == [tool.resource_id]
    assert [item["resourceId"] for item in bindings["mcpServers"]] == [mcp.resource_id]
    assert [item["resourceId"] for item in bindings["skills"]] == [skill.resource_id]


async def test_coordinator_never_injects_ksadk_tools_into_codex_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_client = _AuthoringModelClient(json.dumps(_VALID_PATCH, ensure_ascii=False))
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    tool = studio.catalog.list(kind="tool", limit=1)[0]
    monkeypatch.delenv("KSADK_STUDIO_AUTHORIZER", raising=False)

    result = await studio.compose_agent_conversation(
        messages=_messages(),
        model_profile_id=_model_profile_id(),
        tool_resource_ids=[tool.resource_id],
    )

    assert result["proposal"]["spec"]["bindings"]["tools"] == []


def test_conversation_stages_include_codex_writing() -> None:
    from ksadk.studio.authoring_coordinator import CONVERSATION_STAGES

    assert "codex_writing" in CONVERSATION_STAGES


def test_api_compose_route_exposes_authoring_mode(tmp_path: Path) -> None:
    valid = json.dumps(_VALID_PATCH, ensure_ascii=False)
    model_client = _AuthoringModelClient(valid)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    _install(studio, _FakeExecutor(script=[valid]))
    app = create_studio_app(tmp_path, service=studio, security_enabled=False)
    client = create_test_client(app)
    import os

    old = os.environ.get("KSADK_STUDIO_AUTHORIZER")
    os.environ["KSADK_STUDIO_AUTHORIZER"] = "chat"
    try:
        response = client.post(
            "/api/v1/authoring/conversations:compose",
            json={
                "messages": _messages(),
                "modelProfileId": _model_profile_id(),
            },
        )
    finally:
        if old is None:
            os.environ.pop("KSADK_STUDIO_AUTHORIZER", None)
        else:
            os.environ["KSADK_STUDIO_AUTHORIZER"] = old
    assert response.status_code == 200
    assert response.json()["authoringMode"] == "chat"


def create_test_client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


class _NoFileClient(_FakeCodexClient):
    """模拟"模型只回话不写文件"：只发 agentMessage，不落盘 manifest。"""

    async def run_turn(self, _thread_id: str, prompt: str, *, config=None):
        self.prompts.append(prompt)
        content = self.script.pop(0) if self.script else None
        if content is not None:
            yield {"method": "item/agentMessage/delta", "params": {"delta": content}}
        else:
            yield {"method": "item/agentMessage/delta", "params": {"delta": "抱歉，我无法完成。"}}
        yield {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": {"last": {"inputTokens": 50, "outputTokens": 10, "totalTokens": 60}},
            },
        }


async def test_codex_authoring_recovers_fenced_yaml_from_message(tmp_path: Path) -> None:
    """模型不写文件、只在回复里给 ```yaml 代码块时，兜底解析并落盘。"""

    message = (
        "我先给出配置草案：\n```yaml\n"
        + json.dumps(_VALID_PATCH, ensure_ascii=False, indent=2)
        + "\n```\n如需调整请告诉我。"
    )
    studio = StudioService(tmp_path)
    client = _NoFileClient([message])
    executor = CodexAuthoringExecutor(
        studio.workspace,
        _StaticCredentials(),
        client_factory=lambda env: client,
    )
    result = await executor.compose(
        messages=_messages(),
        model=_fake_model(),
        request_id="req-recover",
    )
    assert result.attempts == 1
    assert result.proposal.slug == "code-review-helper"


async def test_codex_authoring_missing_file_correction_mentions_apply_patch(
    tmp_path: Path,
) -> None:
    executor, client = _executor(tmp_path, [None, json.dumps(_VALID_PATCH, ensure_ascii=False)])
    result = await executor.compose(
        messages=_messages(),
        model=_fake_model(),
        request_id="req-nofile",
    )
    assert result.attempts == 2
    # 缺文件时的纠正提示必须点名 apply_patch 工具调用
    assert "apply_patch" in client.prompts[1]
    # 首轮 builder prompt 也必须包含硬性工具调用要求
    assert "apply_patch" in client.prompts[0]


def test_codex_authoring_default_max_retries_is_three() -> None:
    from ksadk.studio.codex_authoring import DEFAULT_MAX_RETRIES

    assert DEFAULT_MAX_RETRIES == 3


async def test_coordinator_passes_length_retry_without_forcing_tokens(tmp_path: Path) -> None:
    valid = json.dumps(_VALID_PATCH, ensure_ascii=False)
    captured: dict = {}

    class _CapturingClient(_AuthoringModelClient):
        async def complete(self, model, *, messages, **kwargs):
            captured["kwargs"] = kwargs
            captured["model"] = model
            return await super().complete(model, messages=messages, **kwargs)

    studio = StudioService(tmp_path, model_client=_CapturingClient(valid))
    _register_model(studio)
    _install(studio, _FakeExecutor(unavailable=RuntimeError("probe")))
    import os

    old = os.environ.get("KSADK_STUDIO_AUTHORIZER")
    os.environ["KSADK_STUDIO_AUTHORIZER"] = "codex"
    try:
        result = await studio.compose_agent_conversation(
            messages=_messages(),
            model_profile_id=_model_profile_id(),
            request_id="coord-length",
        )
    finally:
        if old is None:
            os.environ.pop("KSADK_STUDIO_AUTHORIZER", None)
        else:
            os.environ["KSADK_STUDIO_AUTHORIZER"] = old
    assert result["authoringMode"] == "chat"
    assert captured["kwargs"].get("retry_on_length") is True
    # 未配置的采样参数不强制注入（None=不发送，服务端默认）
    assert captured["model"].parameters.max_tokens is None
    assert captured["model"].parameters.temperature is None
