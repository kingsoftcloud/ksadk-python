"""PR A：_set_prompt_source_attributes trace 单测（只记 hash/version/count，不记正文）。"""

from __future__ import annotations

from ksadk.conversations.runtime_observability import _set_prompt_source_attributes


class _Span:
    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value


def test_none_compiled_prompt_is_noop() -> None:
    span = _Span()
    _set_prompt_source_attributes(span, None)
    assert span.attrs == {}


def test_records_source_hashes_version_count_not_content() -> None:
    span = _Span()
    compiled = {
        "prompt_section_hashes": {
            "agent_identity": "sha256:sys",
            "agent_policy": "sha256:task",
            "request_instructions": "sha256:req",
        },
        "prompt_platform_policy_version": None,
        "prompt_resolved_sources_version": "v1",
        # 不应被记录的字段（正文 / 凭证）：
        "prompt_content": "SECRET-INSTRUCTION-DO-NOT-LEAK",
    }
    _set_prompt_source_attributes(span, compiled)
    assert span.attrs["prompt.source.agent_system_hash"] == "sha256:sys"
    assert span.attrs["prompt.source.agent_task_hash"] == "sha256:task"
    assert span.attrs["prompt.source.section_count"] == 3
    assert span.attrs["prompt.source.resolved_sources_version"] == "v1"
    # 安全：不含正文。
    dumped = repr(span.attrs)
    assert "SECRET-INSTRUCTION-DO-NOT-LEAK" not in dumped


def test_records_platform_policy_version_when_active() -> None:
    span = _Span()
    _set_prompt_source_attributes(
        span,
        {
            "prompt_section_hashes": {
                "platform_safety": "sha256:safety",
                "agent_identity": "sha256:sys",
            },
            "prompt_platform_policy_version": "env",
            "prompt_resolved_sources_version": "v1",
        },
    )
    assert span.attrs["prompt.source.platform_policy_version"] == "env"
    assert span.attrs["prompt.source.section_count"] == 2
