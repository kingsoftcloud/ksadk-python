"""Keep the privileged input layer bound to host-granted instruction trust."""

from __future__ import annotations

from collections.abc import Mapping


def validate_instruction_authority(
    *, kind: str, trust_level: str, metadata: Mapping[str, object]
) -> None:
    # Names and projection hints are not grants. Recalled/attached data must
    # not enter the compiled prefix or system/developer layer by relabeling it.
    role = metadata.get("role")
    privileged = kind == "compiled_prompt" or (
        isinstance(role, str) and role in {"system", "developer"}
    )
    if privileged and trust_level not in {"platform", "developer"}:
        raise ValueError("context item lacks instruction authority")
