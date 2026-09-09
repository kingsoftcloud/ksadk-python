"""Versioned Scheduler Lite source contracts (P2-06 foundation)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ScheduleModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


_TASK_ID = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
ScheduleOccurrenceState = Literal[
    "claimed",
    "accepted",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]


class ScheduleSpec(ScheduleModel):
    kind: Literal["once", "interval", "cron"]
    timezone: str = "UTC"
    at: datetime | None = None
    every_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    anchor_at: datetime | None = None
    expression: str | None = Field(default=None, min_length=9, max_length=128)
    misfire_policy: Literal["skip", "run_once"] = "skip"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA timezone") from error
        return value

    @field_validator("at", "anchor_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("schedule timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_kind_shape(self) -> "ScheduleSpec":
        if self.kind == "once":
            if self.at is None or self.every_seconds is not None or self.expression is not None:
                raise ValueError("once schedule requires only at")
        elif self.kind == "interval":
            if self.every_seconds is None or self.at is not None or self.expression is not None:
                raise ValueError("interval schedule requires everySeconds and no expression")
        elif self.kind == "cron":
            if self.expression is None or self.at is not None or self.every_seconds is not None:
                raise ValueError("cron schedule requires only expression")
            _parse_cron(self.expression)
        return self


class ScheduledTaskTarget(ScheduleModel):
    # ``agent_instance_id`` identifies the concrete Kernel owner which will
    # consume an Inbox command.  ``agent_id`` is the Studio-facing stable
    # logical Agent identity used to retain a task on its detail page even when
    # the task is pinned to an older immutable Build.  It is optional only so
    # existing v1 SQLite rows remain readable; all Studio-authored tasks set it.
    agent_id: str | None = Field(default=None, min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    agent_instance_id: str = Field(min_length=1, max_length=256)
    agent_version_ref: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    authorization_ref: str = Field(min_length=1, max_length=512)


class ScheduleCommandTemplate(ScheduleModel):
    """The sole supported Scheduler Lite action: durable enqueue via AgentControl."""

    command_type: Literal["enqueue"] = "enqueue"
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self) -> "ScheduleCommandTemplate":
        if "content" not in self.payload:
            raise ValueError("scheduled enqueue payload requires content")
        _reject_clear_secrets(self.payload)
        return self


class ScheduledTask(ScheduleModel):
    api_version: Literal["schedule.ksadk.io/v1"] = "schedule.ksadk.io/v1"
    kind: Literal["ScheduledTask"] = "ScheduledTask"
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=3, max_length=63)
    # A human-facing task label belongs to the durable schedule intent, not a
    # transient Studio card.  Optional keeps the previously frozen v1 rows
    # and third-party producers readable during this additive transition.
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    target: ScheduledTaskTarget
    schedule: ScheduleSpec
    command: ScheduleCommandTemplate
    enabled: bool = True
    continuity: Literal["new_session", "continue_session"] = "new_session"
    concurrency_policy: Literal["forbid"] = "forbid"
    next_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("taskId must be a lowercase stable identifier")
        return value

    @model_validator(mode="after")
    def validate_continuity_target(self) -> "ScheduledTask":
        if self.continuity == "continue_session" and not self.target.session_id:
            raise ValueError("continue_session schedule requires target.sessionId")
        return self

    @field_validator("next_run_at", "created_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("scheduler timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class ScheduleOccurrenceTransition(ScheduleModel):
    """One durable state change displayed by Studio's execution history."""

    state: ScheduleOccurrenceState
    at: datetime
    detail: str | None = Field(default=None, max_length=1024)
    error_code: str | None = Field(default=None, max_length=128)

    @field_validator("at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurrence transition timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class ScheduleOccurrence(ScheduleModel):
    api_version: Literal["schedule.ksadk.io/v1"] = "schedule.ksadk.io/v1"
    kind: Literal["ScheduleOccurrence"] = "ScheduleOccurrence"
    schema_version: Literal[1] = 1
    occurrence_id: str = Field(min_length=8, max_length=256)
    task_id: str = Field(min_length=3, max_length=63)
    # Keep the execution target as an immutable occurrence snapshot.  Tasks can
    # later be edited or deleted, but history/reconciliation must still know
    # exactly which Agent revision accepted this occurrence.
    target: ScheduledTaskTarget | None = None
    scheduled_for: datetime
    session_id: str = Field(min_length=1, max_length=256)
    trigger: Literal["schedule", "manual"] = "schedule"
    state: ScheduleOccurrenceState
    attempt: int = Field(default=1, ge=1, le=100)
    command_id: str | None = None
    accepted_seq: int | None = Field(default=None, ge=0)
    last_event_seq: int | None = Field(default=None, ge=0)
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    claimed_at: datetime | None = None
    accepted_at: datetime | None = None
    started_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=1024)
    completed_at: datetime | None = None
    transitions: tuple[ScheduleOccurrenceTransition, ...] = ()

    @field_validator("scheduled_for", "claimed_at", "accepted_at", "started_at", "completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("occurrence timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> "ScheduleOccurrence":
        terminal = {"succeeded", "failed", "skipped", "cancelled"}
        if self.state in terminal and self.completed_at is None:
            raise ValueError("terminal occurrence requires completedAt")
        if self.state == "failed" and not self.error_code:
            raise ValueError("failed occurrence requires errorCode")
        if self.state == "running" and (not self.run_id or self.started_at is None):
            raise ValueError("running occurrence requires runId and startedAt")
        if self.transitions:
            timestamps = [transition.at for transition in self.transitions]
            if timestamps != sorted(timestamps):
                raise ValueError("occurrence transitions must be chronological")
            if self.transitions[-1].state != self.state:
                raise ValueError("last occurrence transition must match state")
        return self


def _reject_clear_secrets(value: Any, *, key: str = "payload") -> None:
    if isinstance(value, dict):
        for name, item in value.items():
            lowered = str(name).lower()
            if any(term in lowered for term in ("secret", "password", "token", "api_key")):
                if not isinstance(item, str) or not item.startswith(
                    ("env://", "secret://", "credential://", "vault://")
                ):
                    raise ValueError(f"{key}.{name} must be a secret reference, not a value")
            _reject_clear_secrets(item, key=f"{key}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_clear_secrets(item, key=f"{key}[{index}]")


def _parse_cron(expression: str) -> tuple[str, str, str, str, str]:
    fields = tuple(part for part in expression.split() if part)
    if len(fields) != 5:
        raise ValueError("cron expression must use exactly five fields")
    for value in fields:
        if not re.fullmatch(r"[0-9*/,-]+", value):
            raise ValueError("cron fields only support numbers, ranges, lists and steps")
    return fields  # detailed field grammar is shared with calendar.py


__all__ = [
    "ScheduleCommandTemplate",
    "ScheduleOccurrence",
    "ScheduleOccurrenceState",
    "ScheduleOccurrenceTransition",
    "ScheduleSpec",
    "ScheduledTask",
    "ScheduledTaskTarget",
]
