"""Codex-authored conversation proposals.

veadk ``intelligent_development`` 模式的移植：每轮对话创建请求背后是一个真实的
Codex 会话——Codex 在沙箱工作区把 Agent Draft Patch 写成
``.agentkit/authoring/<request_id>/agentkit.yaml`` 文件，产物经过与 chat 链路完全
相同的 ``parse_conversation_proposal`` 验证器校验；校验失败把错误作为下一轮消息
喂回同一个 Codex thread 让它改写文件（最多 ``max_retries`` 轮）。相对 chat 模型
直接输出 JSON，文件产物 + 错误回喂显著降低非法 JSON 残缺 patch 的比例。

Codex 不可用（SDK 缺失、二进制缺失、turn 超时）时抛
``CodexAuthoringUnavailableError``，由 coordinator 降级回 chat 链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from ksadk.studio.authoring import AgentAuthoringService, ConversationProposal
from ksadk.studio.contracts import ResolvedModel, Usage
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace

LOGGER = logging.getLogger(__name__)

#: 单个 Codex turn 的执行超时；超时视为 codex 不可用并降级 chat 链。
DEFAULT_TURN_TIMEOUT_SECONDS = 240.0
#: 校验失败后的最大改写轮数（首轮之外）。
DEFAULT_MAX_RETRIES = 3
#: 失败产物目录保留数量，供诊断；成功目录立即清理。
_MAX_FAILED_DIRECTORIES = 5
_REQUEST_DIR_PREFIX = "codex-req-"

_MANIFEST_FILENAME = "agentkit.yaml"

_BUILDER_SCHEMA = """\
你是 AgentKit Studio 的 Agent 配置编写器。你的唯一任务是把一份 Agent Draft Patch
写成 YAML 文件（不是输出到对话里）。文件必须写到指定路径，使用合法 YAML，根节点
是一个 JSON/YAML 对象，字段如下：

- name: 字符串，Agent 展示名（1-128 字符）
- slug: 字符串，小写字母数字与连字符（1-63 字符）
- runtimeType: 只能是 codex、adk、langgraph
- description: 字符串，可为空（最长 1024）
- spec: 完整 AgentSpec 对象，可包含：
  - instructions（必须）：包含 system（系统提示词）与 task（任务提示词）两个字符串
  - runtime：对象，必须包含 type 字段（与 runtimeType 相同的值：codex/adk/langgraph），
    不要写 provider；adk/langgraph 可加 projectPath/entryPoint/agentVariable
  - execution、context、memory、security、evaluation：按需

完整示例（输出必须严格遵循此结构，字段名一字不差）：

name: 每日科技新闻摘要
slug: daily-tech-news-summary
runtimeType: codex
description: 每天早晨抓取科技新闻源并生成中文简报
spec:
  instructions:
    system: 你是一名资深科技编辑，擅长从多条新闻中提炼要点。
    task: 汇总当日科技新闻，按重要性排序输出中文简报，每条含标题与一句话摘要。
  runtime:
    type: codex

注意：
- 示例中的 name/slug/description/instructions 值必须替换为符合用户
  对话的内容，不要照抄示例文字。
- 模型 Profile、模型参数、Tool、MCP、Skill、凭证、端点和资源 ID 是 Studio 的
  受控输入，严禁写入 model、bindings 或 capabilities。需要它们时只在
  instructions/task 中描述语义用途，Studio 会在确认前注入已选资源。

规则：
0. 硬性要求：你必须在本轮实际调用 apply_patch 工具把完整 patch 写入目标文件，
   然后才能结束回复。只在回复文本里描述计划、或只在回复中给出 YAML/JSON 内容
   （包括 Markdown 代码块）而没有实际写文件，都视为本轮失败。
1. 必须用写文件工具把完整 patch 写入指定路径；不要只在回复中输出内容。
2. 不要输出 Markdown 代码块包裹的 YAML 作为最终答案，文件本身就是产物。
3. 首轮必须包含全部顶层字段；后续轮次（已有草稿时）输出完整合并后的新版本。
4. 不得编造 Tool、MCP、Skill、模型、模型参数、资源 ID、凭证或端点；这些由
   Studio 的资源选择器和策略层注入。
5. 只做配置编写，不要创建其他文件、不要执行无关命令。
"""


class CodexAuthoringUnavailableError(RuntimeError):
    """Codex authoring 后端不可用（缺依赖/二进制、超时），调用方应降级。"""


@dataclass
class CodexAuthoringResult:
    proposal: ConversationProposal
    usage: Usage
    attempts: int
    final_message: str
    request_id: str


@dataclass
class _TurnOutcome:
    final_message: str
    usage: Usage = field(default_factory=Usage)


def _sanitize_request_id(request_id: str | None) -> str:
    candidate = str(request_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", candidate):
        return candidate
    return uuid4().hex


def _extract_usage(params: dict[str, Any]) -> dict[str, int] | None:
    usage = params.get("tokenUsage") or params.get("token_usage")
    if not isinstance(usage, dict):
        return None
    last = usage.get("last")
    if not isinstance(last, dict):
        return None

    def metric(camel: str, snake: str) -> int:
        value = last.get(camel, last.get(snake, 0))
        try:
            return max(0, int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    return {
        "input_tokens": metric("inputTokens", "input_tokens"),
        "output_tokens": metric("outputTokens", "output_tokens"),
        "total_tokens": metric("totalTokens", "total_tokens"),
        "cached_input_tokens": metric("cachedInputTokens", "cached_input_tokens"),
        "reasoning_output_tokens": metric("reasoningOutputTokens", "reasoning_output_tokens"),
    }


class CodexAuthoringExecutor:
    """让真实 Codex 会话在工作区文件里编写 Agent Draft Patch。"""

    def __init__(
        self,
        workspace: Workspace,
        credential_resolver: Any,
        *,
        client_factory: Callable[[dict[str, str]], Any] | None = None,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.workspace = workspace
        self.root = workspace.resolve(".agentkit/authoring")
        self.credentials = credential_resolver
        self._client_factory = client_factory
        self._turn_timeout_seconds = turn_timeout_seconds
        self._max_retries = max(0, int(max_retries))

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def probe(self) -> None:
        """Fail fast when the Codex backend cannot run on this host.

        Raises ``CodexAuthoringUnavailableError`` so the coordinator can
        silently fall back to the chat chain.
        """

        try:
            import openai_codex  # type: ignore[import-not-found] # noqa: F401
        except ImportError as exc:
            raise CodexAuthoringUnavailableError(
                "openai-codex SDK 未安装 (pip install 'ksadk[codex]')"
            ) from exc
        try:
            from codex_cli_bin import bundled_codex_path  # type: ignore[import-not-found, import-untyped]

            binary = Path(str(bundled_codex_path()))
        except (ImportError, OSError) as exc:
            raise CodexAuthoringUnavailableError("本地 Codex 平台二进制不可用") from exc
        if not binary.exists():
            raise CodexAuthoringUnavailableError(f"Codex 二进制不存在: {binary}")

    # ------------------------------------------------------------------
    # Authoring flow
    # ------------------------------------------------------------------

    async def compose(
        self,
        *,
        messages: list[dict[str, str]],
        model: ResolvedModel,
        base: ConversationProposal | None = None,
        request_id: str | None = None,
    ) -> CodexAuthoringResult:
        normalized_request_id = _sanitize_request_id(request_id)
        request_dir = self.root / f"{_REQUEST_DIR_PREFIX}{normalized_request_id}"
        shutil.rmtree(request_dir, ignore_errors=True)
        request_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = request_dir / _MANIFEST_FILENAME

        env = self._codex_env(model)
        client = self._create_client(env)
        usage_total: dict[str, int] = {}
        attempts = 0
        validation_error = ""
        last_message = ""
        try:
            thread_id = await client.start_thread(
                {
                    "cwd": str(request_dir),
                    "sandbox": "workspace-write",
                    "approval_mode": "deny_all",
                    "model": model.model,
                }
            )
            prompt = self._builder_prompt(messages, base=base, manifest_path=manifest_path)
            for attempt in range(self._max_retries + 1):
                attempts = attempt + 1
                if validation_error:
                    prompt = self._correction_prompt(manifest_path, validation_error)
                outcome = await self._run_turn(client, thread_id, prompt)
                last_message = outcome.final_message
                for key, value in outcome.usage.model_dump().items():
                    if key in {"reported", "source"}:
                        continue
                    usage_total[key] = usage_total.get(key, 0) + int(value or 0)
                content = self._read_manifest(manifest_path)
                if content is None:
                    # 兜底：部分模型不调用写文件工具，把 YAML/JSON 直接输出在
                    # agentMessage 里；从 ```yaml/```json 代码块恢复并落盘，
                    # 再走正常校验链，避免整轮作废。
                    recovered = self._recover_manifest_from_message(last_message)
                    if recovered is not None:
                        LOGGER.warning(
                            "codex authoring attempt %d wrote no manifest; recovered "
                            "fenced block from agent message: requestId=%s",
                            attempts,
                            normalized_request_id,
                        )
                        try:
                            manifest_path.write_text(recovered, encoding="utf-8")
                            content = recovered
                        except OSError:
                            LOGGER.warning(
                                "codex authoring fallback write failed: requestId=%s",
                                normalized_request_id,
                            )
                if content is not None:
                    # Codex 习惯写 YAML；parse_conversation_proposal 的 JSON 提取器
                    # 遇到 YAML 里游离的 `{}` 会误解析成空 patch，这里先归一成 JSON。
                    try:
                        yaml_payload = yaml.safe_load(content)
                    except yaml.YAMLError:
                        yaml_payload = None
                    if isinstance(yaml_payload, dict):
                        content = json.dumps(yaml_payload, ensure_ascii=False)
                if content is None:
                    validation_error = (
                        f"{_MANIFEST_FILENAME} 文件不存在或为空；你上一轮没有写文件，"
                        f"必须调用 apply_patch 工具（*** Begin Patch … Add File … "
                        f"*** End Patch）把完整 patch 写入 {manifest_path}，"
                        "禁止只在回复文本中输出内容或 Markdown 代码块"
                    )
                    LOGGER.warning(
                        "codex authoring attempt %d produced no manifest: requestId=%s",
                        attempts,
                        normalized_request_id,
                    )
                    continue
                try:
                    proposal = AgentAuthoringService.parse_conversation_proposal(content, base=base)
                except StudioError as exc:
                    validation_error = str(exc.details.get("reason") or exc.message)
                    LOGGER.warning(
                        "codex authoring attempt %d invalid: requestId=%s reason=%s",
                        attempts,
                        normalized_request_id,
                        validation_error,
                    )
                    continue
                self._cleanup_request_dir(request_dir, success=True)
                return CodexAuthoringResult(
                    proposal=proposal,
                    usage=Usage(**usage_total, reported=True, source="codex-authoring"),
                    attempts=attempts,
                    final_message=last_message,
                    request_id=normalized_request_id,
                )
            raise StudioError(
                "AUTHORING_MODEL_OUTPUT_INVALID",
                "Codex 会话没有产出合法的 Agent Draft Patch",
                status_code=502,
                details={
                    "validationError": validation_error,
                    "attemptedCorrections": attempts - 1,
                    "requestId": normalized_request_id,
                },
            )
        finally:
            try:
                await client.close()
            except Exception:  # pragma: no cover - defensive close
                LOGGER.debug("codex authoring client close failed", exc_info=True)
            self._cleanup_request_dir(request_dir, success=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create_client(self, env: dict[str, str]) -> Any:
        if self._client_factory is not None:
            return self._client_factory(env)
        try:
            from openai_codex import CodexConfig  # type: ignore[import-not-found]

            from ksadk.codex.client import AsyncCodexClient
        except ImportError as exc:
            raise CodexAuthoringUnavailableError(
                "openai-codex SDK 未安装 (pip install 'ksadk[codex]')"
            ) from exc
        return AsyncCodexClient(CodexConfig(env=env))

    def _codex_env(self, model: ResolvedModel) -> dict[str, str]:
        env: dict[str, str] = {}
        try:
            credential = self.credentials.resolve(model.credential_ref)
            env["OPENAI_API_KEY"] = credential
        except StudioError:
            raise CodexAuthoringUnavailableError(f"无法解析模型凭证引用: {model.credential_ref}")
        base_url = str(model.endpoint_url or "").rstrip("/")
        for suffix in ("/chat/completions", "/responses"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
            env["OPENAI_API_BASE"] = base_url
        env["OPENAI_MODEL_NAME"] = model.model
        return env

    def _builder_prompt(
        self,
        messages: list[dict[str, str]],
        *,
        base: ConversationProposal | None,
        manifest_path: Path,
    ) -> str:
        parts = [_BUILDER_SCHEMA, f"\n目标文件路径（绝对路径）：{manifest_path}\n"]
        if base is not None:
            current = base.model_dump(by_alias=True, mode="json", exclude_none=True)
            parts.append(
                "当前已有草稿（作为起点，必须合并用户最新要求后输出完整新版本）：\n"
                + yaml.safe_dump(current, allow_unicode=True, sort_keys=False)
            )
        transcript = "\n".join(
            f"{str(item.get('role') or 'user')}: {str(item.get('content') or '').strip()}"
            for item in messages
        )
        parts.append(f"用户对话（最后一条 user 消息是最新要求，必须优先满足）：\n{transcript}\n")
        parts.append("现在把完整的 Agent Draft Patch 写入目标文件。")
        parts.append(
            "写入方式要求：沙箱外命令已被审批策略拒绝，不要尝试 shell 重定向"
            "（如 cat > file 或 echo > file）——它们会被静默拒绝导致文件不存在。"
            "必须使用 apply_patch 工具（*** Begin Patch … Add File: <绝对路径> … "
            "*** End Patch）把完整 YAML 内容写为目标文件。"
        )
        return "\n".join(parts)

    @staticmethod
    def _correction_prompt(manifest_path: Path, validation_error: str) -> str:
        return (
            f"你写入的 {_MANIFEST_FILENAME} 未通过 Agent Draft Patch 校验。\n"
            f"校验错误细节如下，请逐条修正后重写文件 {manifest_path}（完整内容，"
            "不是增量补丁）：\n"
            f"{validation_error}"
        )

    async def _run_turn(self, client: Any, thread_id: str, prompt: str) -> _TurnOutcome:
        message_parts: list[str] = []
        usage = Usage()
        event_counts: dict[str, int] = {}
        started = time.monotonic()
        try:
            # asyncio.timeout 是 3.11+；项目基线 3.10，用 wait_for 包装整轮消费。
            async def _consume() -> None:
                nonlocal usage
                async for event in client.run_turn(thread_id, prompt):
                    if not isinstance(event, dict):
                        continue
                    method = str(event.get("method") or "")
                    event_counts[method] = event_counts.get(method, 0) + 1
                    params = event.get("params") or {}
                    if not isinstance(params, dict):
                        continue
                    if method == "item/agentMessage/delta":
                        message_parts.append(str(params.get("delta") or ""))
                    elif method == "thread/tokenUsage/updated":
                        metrics = _extract_usage(params)
                        if metrics:
                            usage = Usage(
                                **metrics,  # type: ignore[arg-type]
                                reported=True,
                            )

            await asyncio.wait_for(_consume(), timeout=self._turn_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise CodexAuthoringUnavailableError(
                f"Codex authoring turn 超时（{self._turn_timeout_seconds:.0f}s）"
            ) from exc
        LOGGER.info("codex authoring turn finished in %.2fs", time.monotonic() - started)
        # 事件 method 分布：诊断"模型只回话不调工具"（无 item/*command* 事件）等
        # 失败模式的关键证据，debug 级别避免常态噪音。
        LOGGER.debug("codex authoring turn events: %s", dict(sorted(event_counts.items())))
        return _TurnOutcome(final_message="".join(message_parts), usage=usage)

    _FENCED_BLOCK_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)```", re.DOTALL)

    @classmethod
    def _recover_manifest_from_message(cls, message: str) -> str | None:
        """从 agentMessage 文本里恢复 patch：优先围栏代码块，其次裸 JSON 对象。"""

        text = str(message or "").strip()
        if not text:
            return None
        for match in cls._FENCED_BLOCK_RE.finditer(text):
            block = match.group(1).strip()
            if block:
                return block
        if text.startswith("{") and text.endswith("}"):
            return text
        return None

    @staticmethod
    def _read_manifest(manifest_path: Path) -> str | None:
        try:
            content = manifest_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return content or None

    def _cleanup_request_dir(self, request_dir: Path, *, success: bool) -> None:
        if success:
            shutil.rmtree(request_dir, ignore_errors=True)
            return
        if not request_dir.exists():
            return
        failed = sorted(
            (path for path in self.root.glob(f"{_REQUEST_DIR_PREFIX}*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
        )
        while len(failed) > _MAX_FAILED_DIRECTORIES:
            shutil.rmtree(failed.pop(0), ignore_errors=True)


__all__ = [
    "CodexAuthoringExecutor",
    "CodexAuthoringResult",
    "CodexAuthoringUnavailableError",
]
