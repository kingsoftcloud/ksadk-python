"""Teams v1 inputs and trusted identities, independent of Studio and Providers.

Actor is constructed by the host/verified tool invocation, never decoded from
browser JSON. Public names mirror the shared Web Teams contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_VERSION = "teams.ksadk.io/v1"
PLUGIN_VERSION = "0.1.0"
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    subject: str
    kind: Literal["human", "member", "host"] = "human"
    group_id: str | None = None
    member_id: str | None = None
    team_run_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MemberInput(InputModel):
    memberId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    bindingRef: str = Field(min_length=1, max_length=256)


class GroupCreateInput(InputModel):
    name: str = Field(min_length=1, max_length=120)
    members: list[MemberInput] = Field(min_length=1, max_length=8)
    leaderMemberId: str
    idempotencyKey: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_roster(self) -> GroupCreateInput:
        ids = [member.memberId for member in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("团队成员标识不能重复")
        if self.leaderMemberId not in ids:
            raise ValueError("Leader 必须属于已选成员")
        return self


class MessagePart(InputModel):
    kind: Literal["text", "attachment"]
    text: str | None = Field(default=None, max_length=100_000)
    attachmentRef: str | None = Field(default=None, max_length=256)
    mediaType: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_part(self) -> MessagePart:
        if self.kind == "text" and not self.text:
            raise ValueError("消息正文不能为空")
        if self.kind == "attachment" and (not self.attachmentRef or not self.mediaType):
            raise ValueError("附件需要已授权的引用和类型")
        return self


class MessageInput(InputModel):
    parts: list[MessagePart] = Field(min_length=1, max_length=32)
    mentions: list[str] = Field(default_factory=list, max_length=8)
    intent: Literal["start_goal", "followup", "directed", "note"]
    idempotencyKey: str = Field(min_length=1, max_length=200)
    replyTo: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_targets(self) -> MessageInput:
        self.mentions = list(dict.fromkeys(self.mentions))
        if self.intent == "directed" and not self.mentions:
            raise ValueError("定向消息需要选择成员")
        return self


class Budget(InputModel):
    maxMembers: int = Field(default=8, ge=1, le=8)
    maxConcurrent: int = Field(default=4, ge=1, le=8)
    maxStarts: int = Field(default=32, ge=1, le=256)
    startsUsed: int = Field(default=0, ge=0)
    maxHops: int = Field(default=8, ge=1, le=32)
    maxTokens: int = Field(default=100_000, ge=1, le=10_000_000)
    tokensUsed: int = Field(default=0, ge=0)
    maxDurationSeconds: int = Field(default=3600, ge=30, le=86400)


class TaskCreateInput(InputModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    ownerMemberId: str | None = None
    dependencies: list[str] = Field(default_factory=list, max_length=64)
    acceptanceCriteria: str = Field(default="提交可核验结果", max_length=5000)
    acceptancePolicy: Literal["human", "result"] = "human"


class ControlInput(InputModel):
    action: Literal["suspend_dispatch", "resume_dispatch", "stop"]
    expectedRevision: int = Field(ge=1)
    idempotencyKey: str = Field(min_length=1, max_length=200)


def plain_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(part.get("text", ""))
        if part.get("kind") == "text"
        else f"附件：{part.get('name') or '交付物'}（artifactId={part.get('attachmentRef')}）"
        for part in parts
    )
