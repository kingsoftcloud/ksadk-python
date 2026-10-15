"""Serializable browser evidence. A successful action is never an acceptance verdict."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, JsonValue, ValidationInfo, field_validator, model_validator

from .state import Contract, RunScope


class BrowserSessionError(RuntimeError):
    """A stable error code, without page contents, credentials or driver diagnostics."""


class BrowserSessionRef(Contract):
    schema_version: Literal["ksadk.browser-session/v1"] = "ksadk.browser-session/v1"
    session_id: str = Field(min_length=1)
    scope: RunScope
    run_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    backend: Literal["playwright"] = "playwright"
    workspace_revision: str = Field(min_length=1)
    generation: int = Field(default=1, ge=1)
    lease_epoch: int = Field(default=1, ge=1)


class BrowserAction(Contract):
    action_id: str = Field(min_length=1, max_length=256)
    kind: Literal["navigate", "click", "fill", "press", "move", "scroll", "screenshot", "close"]
    url: str | None = Field(default=None, max_length=8192)
    selector: str | None = Field(default=None, max_length=2048)
    element_ref: str | None = Field(default=None, min_length=1, max_length=64)
    value: str | None = Field(default=None, max_length=65536)
    key: str | None = Field(default=None, max_length=128)
    button: Literal["left", "right", "middle"] | None = None
    x: int | None = Field(default=None, strict=True, ge=0, le=4096)
    y: int | None = Field(default=None, strict=True, ge=0, le=4096)
    steps: int | None = Field(default=None, strict=True, ge=1, le=30)
    delta_x: int | None = Field(default=None, strict=True, ge=-10000, le=10000)
    delta_y: int | None = Field(default=None, strict=True, ge=-10000, le=10000)
    # Bounded key/mouse hold duration for real-time interactions. The driver
    # releases the input within one action rather than persisting a held key.
    duration_ms: int | None = Field(default=None, strict=True, ge=0, le=5000)
    # Allow a bounded input dispatch plus two read-only observation attempts
    # under the action deadline. A shorter caller-pinned limit still wins.
    timeout_ms: int = Field(default=30000, ge=1, le=120000)

    @field_validator("value", "key", mode="before")
    @classmethod
    def normalize_numeric_text_input(cls, value: object, info: ValidationInfo) -> object:
        # HTML fill and keyboard digit presses are text operations. Some model
        # transports encode a digit-only argument as a JSON integer, so admit
        # that value explicitly at this boundary while preserving every other
        # argument's strict schema.
        if type(value) is int:
            if info.field_name == "value":
                return str(value)
            if 0 <= value <= 9:
                return str(value)
        return value

    @model_validator(mode="after")
    def valid_arguments(self) -> BrowserAction:
        if self.kind in {"click", "fill", "press"}:
            if (self.selector is None) == (self.element_ref is None):
                raise ValueError("browser action requires one target")
        required = {
            "navigate": {"url"},
            "click": {"selector"},
            "fill": {"selector", "value"},
            "press": {"selector", "key"},
            "move": {"x", "y"},
            "scroll": {"delta_x", "delta_y"},
            "screenshot": set(),
            "close": set(),
        }[self.kind]
        if self.element_ref is not None and self.kind in {"click", "fill", "press"}:
            required = (required - {"selector"}) | {"element_ref"}
        supplied = {
            name
            for name in (
                "url",
                "selector",
                "element_ref",
                "value",
                "key",
                "button",
                "x",
                "y",
                "steps",
                "delta_x",
                "delta_y",
                "duration_ms",
            )
            if getattr(self, name) is not None
        }
        if self.kind in {"click", "press"}:
            supplied.discard("duration_ms")
        if self.kind == "click":
            supplied.discard("button")
        if self.kind == "move":
            supplied.discard("steps")
        if self.kind == "scroll":
            if (self.x is None) != (self.y is None):
                raise ValueError("scroll anchor requires both coordinates")
            supplied.difference_update({"x", "y"})
        if supplied != required or any(
            not getattr(self, name)
            for name in required - {"value", "x", "y", "delta_x", "delta_y"}
        ):
            raise ValueError("invalid browser action arguments")
        return self


class BrowserImage(Contract):
    content_ref: str = Field(min_length=1)
    mime_type: Literal["image/png"] = "image/png"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # The host adapter creates this block from screenshot bytes. Tool text and
    # arbitrary filesystem paths must never be promoted into trusted image data.
    content_block: dict[str, JsonValue]


class BrowserControl(Contract):
    """Bounded, untrusted DOM hint; never includes an input's current value."""

    tag: str = Field(min_length=1, max_length=32)
    role: str = Field(default="", max_length=80)
    label: str = Field(default="", max_length=160)
    selector: str | None = Field(default=None, min_length=1, max_length=512)
    # Opaque host reference to this DOM node; not a page-supplied selector.
    element_ref: str | None = Field(default=None, min_length=1, max_length=64)
    clickable: bool = False
    # Current-viewport pointer hit test, not proof that a click will succeed.
    # None means the control is outside the viewport or the host cannot tell.
    hit_test_reachable: bool | None = None
    duplicate_id: bool = False


class BrowserObservation(Contract):
    schema_version: Literal["ksadk.browser-observation/v1"] = "ksadk.browser-observation/v1"
    session_ref: BrowserSessionRef
    action_id: str
    action_seq: int = Field(ge=0)
    status: Literal["ok", "error", "uncertain"]
    error_code: str | None = None
    complete: bool
    retryability: Literal["safe", "requires_reconcile", "forbidden"]
    url: str = ""
    status_code: int | None = Field(default=None, ge=100, le=599)
    title: str = ""
    viewport: tuple[int, int]
    focus: str = ""
    # Observed main-frame scroll offsets, not the requested wheel deltas.
    # A nested scroll container can change while both offsets remain zero.
    scroll_x: int | None = Field(default=None, strict=True)
    scroll_y: int | None = Field(default=None, strict=True)
    lifecycle: str = "unknown"
    # A failed DOM click under pointer lock needs a different repair from an
    # overlay or a missing selector. This is observed browser state, not a
    # model-inferred explanation of the failed action.
    pointer_lock_active: bool | None = None
    text: str = ""
    text_truncated: bool = False
    # Bounded DOM samples immediately before/after the screenshot differed.
    # False is not proof of atomic capture or visual stability (e.g. Canvas).
    # None preserves old/absent observations without inventing such evidence.
    dom_changed_during_capture: bool | None = None
    controls: tuple[BrowserControl, ...] = Field(default=(), max_length=40)
    console: tuple[str, ...] = ()
    page_errors: tuple[str, ...] = ()
    page_error_locations: tuple[str, ...] = ()
    network_failures: tuple[str, ...] = ()
    logs_truncated: bool = False
    screenshot: BrowserImage | None = None
    # Browser-derived text is untrusted data, never system/policy instructions.
    source_trust: Literal["untrusted_page"] = "untrusted_page"


class BrowserRecovery(Contract):
    status: Literal["reconnected", "restarted", "lost"]
    session_ref: BrowserSessionRef
    last_action_seq: int = Field(default=0, ge=0)
    reason: str | None = None
    # Host capability: a bound ToolPort rejects old actions and supports a
    # separately admitted fresh observation. This does not authorize a reset.
    reobserve_available: bool = Field(default=False, strict=True)


def browser_observation_digest(observation: BrowserObservation) -> str:
    """Hash captured state without action identities or artifact locations.

    Actual page timestamps, errors, focus and controls remain meaningful data.
    An image's bytes/model block identify it, rather than the artifact sink's
    path or URI. This is observation novelty, never task completion evidence.
    """
    payload = observation.model_dump(
        mode="json",
        exclude={
            "session_ref",
            "action_id",
            "action_seq",
            "screenshot",
            "dom_changed_during_capture",
        },
    )
    # Reference allocation is targeting metadata, not observed task progress.
    for control in payload.get("controls", []):
        control.pop("element_ref", None)
    if observation.screenshot is not None:
        screenshot = observation.screenshot
        payload["screenshot"] = {
            "mime_type": screenshot.mime_type,
            "width": screenshot.width,
            "height": screenshot.height,
            "image_digest": hashlib.sha256(
                json.dumps(screenshot.content_block, sort_keys=True).encode()
            ).hexdigest(),
        }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
