from __future__ import annotations

import os
import re
from typing import Any, Sequence

import httpx

from ksadk.configs.settings import settings
from ksadk.conversations.model_options import model_options_for_chat_completions
from ksadk.conversations.reasoning_markup import strip_reasoning_markup

DEFAULT_SESSION_TITLE_TIMEOUT_MS = 8_000
SESSION_TITLE_MAX_CHARS = 24
HEURISTIC_SESSION_TITLE_SOURCE = "heuristic"
SESSION_TITLE_SOURCE_SCAN_LIMIT = 8_000

_TITLE_PROMPT = (
    "你是会话标题生成器。"
    "请基于首轮用户问题和首轮助手回答，生成一个适合左侧会话列表的中文标题。\n"
    "要求：\n"
    "1. 优先输出 4 到 8 个汉字，最多 12 个汉字\n"
    "2. 输出主题名或任务名，不要直接复述整句提问\n"
    "3. 去掉“你好”“请”“帮我”“看看这个”等口语前缀和文件名\n"
    "4. 如果是自我介绍/能力说明，改写成“能力介绍”“功能概览”这类主题名\n"
    "5. 如果是在看图或看附件，改写成“架构图分析”“附件分析”“简历分析”这类主题名\n"
    "6. 不要标点、引号、书名号、解释，只输出标题本身\n"
    "示例：\n"
    "- 你好，请介绍一下你自己 -> 招聘助手能力\n"
    "- 看看这个上传文件，这里还有他画的架构图 -> 架构图分析"
)

_SELF_INTRO_RE = re.compile(r"(介绍(?:一下)?你自己|你是谁|你的能力|你能做什么|自我介绍)")
_ARCH_RE = re.compile(r"(架构图|架构|微服务|系统设计|服务拓扑)")
_RECRUIT_RE = re.compile(r"(简历|候选人|面试|招聘|职位|jd\b)")
_FILE_RE = re.compile(r"(上传文件|上传图片|附件|file|pdf|png|jpg|jpeg)")
_ANALYZE_RE = re.compile(r"(分析|解读|评审|review|总结|说明|看下|看看|评估)")
_PROMPT_FILLERS_RE = re.compile(
    r"^(你好|您好|请问|请|帮我|麻烦|看看这个|看看|看下|分析一下|分析|介绍一下|介绍|解释一下|解释|总结一下|总结|直接开始分析吧|直接开始|这里还有|这边有|给我看下)+",
)
_FILE_MARKUP_RE = re.compile(
    r"(\[[^\]]*(上传文件|上传文件引用|附件)[^\]]*\]|#+\s*附件\s*[-:：]\s*[^\n]+)",
    re.IGNORECASE,
)


def _normalize_source_text(text: str) -> str:
    value = strip_reasoning_markup(str(text or "")[:SESSION_TITLE_SOURCE_SCAN_LIMIT]).strip()
    if not value:
        return ""
    value = _FILE_MARKUP_RE.sub(" 附件 ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _sanitize_title(text: str) -> str:
    value = strip_reasoning_markup(str(text or "")).strip()
    if not value:
        return ""
    value = value.splitlines()[0].strip()
    value = re.sub(r"^标题[:：]\s*", "", value)
    value = value.strip("`'\"“”‘’《》[]()（）")
    value = re.sub(r"[。！？!?；;：:，,、]+$", "", value).strip()
    value = " ".join(value.split())
    if len(value) > SESSION_TITLE_MAX_CHARS:
        value = value[:SESSION_TITLE_MAX_CHARS].rstrip()
    return value


def _normalize_compare_text(text: str) -> str:
    value = _normalize_source_text(text)
    value = _PROMPT_FILLERS_RE.sub("", value).strip()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    return value.lower()


def _truncate_title(text: str) -> str:
    value = strip_reasoning_markup(str(text or "")).strip()
    if len(value) <= SESSION_TITLE_MAX_CHARS:
        return value
    return value[:SESSION_TITLE_MAX_CHARS].rstrip()


def build_fallback_title(text: str) -> str:
    value = _normalize_source_text(text)
    value = _FILE_MARKUP_RE.sub(" 附件 ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return _truncate_title(value)


def build_heuristic_title(*, first_prompt: str, assistant_text: str) -> str:
    prompt = _normalize_source_text(first_prompt)
    assistant = _normalize_source_text(assistant_text)
    combined = f"{prompt} {assistant}".strip()
    prompt_lower = prompt.lower()
    combined_lower = combined.lower()

    if _SELF_INTRO_RE.search(prompt):
        if _RECRUIT_RE.search(combined_lower):
            return "招聘助手能力"
        return "Agent能力介绍"

    if _ARCH_RE.search(combined):
        return "架构图分析" if _ANALYZE_RE.search(combined) or assistant else "架构讨论"

    if _RECRUIT_RE.search(combined_lower):
        if "简历" in combined_lower or "候选人" in combined_lower:
            return "简历分析"
        if "面试" in combined_lower and (_ANALYZE_RE.search(combined) or _FILE_RE.search(combined_lower)):
            return "面试分析"

    if _FILE_RE.search(combined_lower):
        return "附件分析" if _ANALYZE_RE.search(combined) or assistant else "附件内容"

    candidate = _PROMPT_FILLERS_RE.sub("", prompt).strip("，,。！？!?；;：: ")
    candidate = re.sub(r"^(这个|这份|这张|这段|这个上传文件|上传文件|附件)\s*", "", candidate)
    candidate = candidate.strip("，,。！？!?；;：: ")
    if _SELF_INTRO_RE.search(candidate):
        return "自我介绍"
    if candidate.endswith("架构图") or candidate == "架构图":
        return "架构图分析"
    if _FILE_RE.search(candidate.lower()):
        return "附件分析"
    return _truncate_title(candidate)


def is_low_quality_title(title: str, *, first_prompt: str) -> bool:
    cleaned_title = _sanitize_title(title)
    if not cleaned_title:
        return True
    prompt_norm = _normalize_compare_text(first_prompt)
    title_norm = _normalize_compare_text(cleaned_title)
    if not title_norm:
        return True
    if title_norm == prompt_norm:
        return True
    if prompt_norm.startswith(title_norm) and len(prompt_norm) - len(title_norm) <= 4:
        return True
    if title_norm.startswith(("你好", "请", "帮我", "看看这个")):
        return True
    return False


def resolve_session_title_model(current_model: str | None) -> str:
    override = str(os.getenv("SESSION_TITLE_MODEL", "")).strip()
    if override:
        return override
    if current_model:
        return str(current_model).strip()
    return str(settings.model.model_name or "").strip()


def build_session_title_messages(*, first_prompt: str, assistant_text: str) -> list[dict[str, str]]:
    assistant_excerpt = strip_reasoning_markup(str(assistant_text or "")).strip()
    if len(assistant_excerpt) > 240:
        assistant_excerpt = assistant_excerpt[:240].rstrip() + "…"
    return [
        {"role": "system", "content": _TITLE_PROMPT},
        {
            "role": "user",
            "content": (
                f"首轮用户问题：\n{str(first_prompt or '').strip()}\n\n"
                f"首轮助手回答：\n{assistant_excerpt}"
            ),
        },
    ]


class SessionTitleClient:
    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        self.api_base = str(api_base or settings.model.api_base or "").rstrip("/")
        self.api_key = str(api_key or settings.model.api_key or "").strip()

    @property
    def is_available(self) -> bool:
        return bool(self.api_base and self.api_key)

    def _chat_completions_url(self) -> str:
        if self.api_base.endswith("/v1"):
            return f"{self.api_base}/chat/completions"
        return f"{self.api_base}/v1/chat/completions"

    async def generate_title(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, str]],
        timeout_ms: int = DEFAULT_SESSION_TITLE_TIMEOUT_MS,
    ) -> tuple[str, dict[str, Any]]:
        if not self.is_available:
            raise RuntimeError("session title client is not configured")
        if not model:
            raise RuntimeError("session title model is not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0,
            **model_options_for_chat_completions({"thinking": {"type": "disabled"}}),
        }
        timeout_seconds = max(1.0, float(timeout_ms) / 1000.0)
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(self._chat_completions_url(), headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        message = choices[0].get("message") if choices else {}
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, list):
            fragments: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    fragments.append(str(item["text"]))
            content = "\n".join(fragment for fragment in fragments if fragment)

        title = _sanitize_title(str(content or ""))
        if not title:
            raise RuntimeError("session title model returned empty content")
        return title, dict(data.get("usage") or {})


def resolve_session_title_client() -> SessionTitleClient:
    return SessionTitleClient()
