"""Locked browser action contract, shared by model projection and execution."""

from ..native.ports import ToolDefinition


def browser_tool_definition() -> ToolDefinition:
    session = {
        "session_id": {"type": "string", "minLength": 1},
        "generation": {"type": "integer", "minimum": 1},
    }
    timeout = {"timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000}}

    def action_schema(
        action: str, required: tuple[str, ...] = (), optional_timeout: bool = True
    ) -> dict:
        fields = {
            "action": {"const": action},
            **({} if action == "open" else session),
            **({} if not optional_timeout else timeout),
            **{name: properties[name] for name in required},
            **(
                {"duration_ms": properties["duration_ms"]}
                if action in {"click", "press"}
                else {}
            ),
            **({"button": properties["button"]} if action == "click" else {}),
            **({"steps": properties["steps"]} if action == "move" else {}),
            **({name: properties[name] for name in ("x", "y")} if action == "scroll" else {}),
            **(
                {"element_ref": properties["element_ref"]}
                if action in {"click", "fill", "press"} else {}
            ),
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", *([] if action == "open" else session),
                         *(name for name in required if name != "selector")],
            "properties": fields,
            **({"oneOf": [{"required": ["selector"]}, {"required": ["element_ref"]}]}
               if action in {"click", "fill", "press"} else {}),
            **({"dependentRequired": {"x": ["y"], "y": ["x"]}} if action == "scroll" else {}),
        }

    properties = {
        "url": {"type": "string", "minLength": 1},
        "selector": {"type": "string", "minLength": 1},
        "element_ref": {"type": "string", "minLength": 1, "maxLength": 64},
        "value": {"type": ["string", "integer"]},
        "key": {"type": ["string", "integer"], "minimum": 0, "maximum": 9, "minLength": 1},
        "button": {"enum": ["left", "right", "middle"]},
        "x": {"type": "integer", "minimum": 0, "maximum": 4096},
        "y": {"type": "integer", "minimum": 0, "maximum": 4096},
        "steps": {"type": "integer", "minimum": 1, "maximum": 30},
        "delta_x": {"type": "integer", "minimum": -10000, "maximum": 10000},
        "delta_y": {"type": "integer", "minimum": -10000, "maximum": 10000},
        "duration_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
    }
    return ToolDefinition(
        name="native_browser",
        observation_role="browser",
        description=(
            "Operate a persistent scoped browser. First call open with only action; "
            "it creates a blank session and returns session_ref. Then call navigate "
            "with the URL, session_id and generation from that ref. Reuse the same "
            "session_id and generation for click, fill, press, move, scroll, screenshot and close. "
            "For fill, value is text; an integer is accepted as its decimal text. "
            "For press, key is a string or a single digit integer. "
            "For click/fill/press, use exactly one of element_ref from returned controls "
            "or a CSS selector. Prefer element_ref for an observed control: it targets the "
            "same DOM node after reordering; a stale ref returns a fresh observation. "
            "Click and press can hold their input for duration_ms (0–5000) before release. "
            "Click can choose left/right/middle button; move uses viewport CSS pixel x/y. "
            "Scroll uses signed delta_x/delta_y CSS pixels, optionally at an x/y viewport point. "
            "Wheel dispatch does not prove scrolling settled; inspect the returned observation. "
            "Observations include screenshots; action success is not task acceptance."
        ),
        parameters={
            "type": "object",
            "oneOf": [
                action_schema("open", optional_timeout=False),
                action_schema("navigate", ("url",)),
                action_schema("click", ("selector",)),
                action_schema("fill", ("selector", "value")),
                action_schema("press", ("selector", "key")),
                action_schema("move", ("x", "y")),
                action_schema("scroll", ("delta_x", "delta_y")),
                action_schema("screenshot"),
                action_schema("close"),
            ],
        },
    )
