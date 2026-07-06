"""run_status 事件的 run_mode / run_trigger 枚举常量。

canonical 定义，供 ksadk runtime 与 server 各自维护同名常量保持一致。
两个维度独立：
- run_mode：怎么跑（background/foreground/unknown）
- run_trigger：怎么开始（new_run/checkpoint_resume/approval_resume/unknown）
"""

from __future__ import annotations

from typing import Literal, Mapping

RunMode = Literal["background", "foreground", "unknown"]
RunTrigger = Literal["new_run", "checkpoint_resume", "approval_resume", "unknown"]

RUN_MODE_BACKGROUND = "background"
RUN_MODE_FOREGROUND = "foreground"
RUN_MODE_UNKNOWN = "unknown"

RUN_TRIGGER_NEW_RUN = "new_run"
RUN_TRIGGER_CHECKPOINT_RESUME = "checkpoint_resume"
RUN_TRIGGER_APPROVAL_RESUME = "approval_resume"
RUN_TRIGGER_UNKNOWN = "unknown"

_VALID_RUN_MODES = {"background", "foreground", "unknown"}
_VALID_RUN_TRIGGERS = {"new_run", "checkpoint_resume", "approval_resume", "unknown"}


def validate_run_mode(value: str | None) -> str:
    """非法值或 None 降级为 unknown，避免脏值落库。"""
    return value if value in _VALID_RUN_MODES else RUN_MODE_UNKNOWN


def validate_run_trigger(value: str | None) -> str:
    """非法值或 None 降级为 unknown，避免脏值落库。"""
    return value if value in _VALID_RUN_TRIGGERS else RUN_TRIGGER_UNKNOWN


def trigger_from_resume_input(resume_input: dict | Mapping | None) -> str:
    """从 resume_input 推导 run_trigger。

    - checkpoint resume（agentengine.resume_checkpoint）→ checkpoint_resume
    - approval resume（mcp_approval_response）→ approval_resume
    - None / 无 type → new_run
    - 其他未知 type → unknown
    """
    if resume_input is None:
        return RUN_TRIGGER_NEW_RUN
    resume_type = str(resume_input.get("type") or "").strip()
    if resume_type == "agentengine.resume_checkpoint":
        return RUN_TRIGGER_CHECKPOINT_RESUME
    if resume_type == "mcp_approval_response":
        return RUN_TRIGGER_APPROVAL_RESUME
    if not resume_type:
        return RUN_TRIGGER_NEW_RUN
    return RUN_TRIGGER_UNKNOWN
