"""Host-authorized scoped media carrier for delegated Native child runs.

A delegated child receives its task as prose (``ChildRunRequest.task``). This
module carries a parent's already-admitted user image to the child run without
ever trusting model-supplied bytes, paths, URLs, data URLs or another run's
media.

The model may only name an authorized source and a position within it
(``ChildMediaRef``). The host resolves that reference against the parent run's
current user input, extracts the original immutable image content, and builds
the child's ``NativeUserInput``. It fails closed on unknown, forged or foreign
references and on an image-incapable child role model, before any child model
HTTP is dispatched.

This is a pure data resolver: it performs no I/O, holds no credentials, and
never reads files or fetches remote URLs. Image bytes stay the exact
``ModelImageContent`` the parent admitted.
"""

from __future__ import annotations

from .model import ModelImageContent, ModelProfile, ModelTextContent
from .state import RunSnapshot
from .subagents import ChildMediaRef
from .user_input import NativeUserInput, matches_user_goal

# The single authorized media source. A child can only receive media that the
# host already admitted for this parent run's current user input; there is no
# source for another run's media, a model-supplied path, URL or data URL.
PARENT_USER_INPUT_SOURCE = "parent_user_input"

# Mirror the carrier's declared finite reference bound. A larger request is a
# malformed authorization, not a wider media grant.
_MAX_MEDIA_REFS = 20


def _parent_user_owner_message(parent: RunSnapshot) -> dict:
    """The parent run's current user input message (its authorized media owner).

    The current turn is the last plain user message whose text matches the
    parent goal. Historical user turns, document observations and tool receipts
    are not media owners. A parent with no current user input admits no media.
    """
    for message in reversed(parent.messages):
        if (
            message.get("role") == "user"
            and message.get("kind") is None
            and matches_user_goal(message.get("content"), parent.goal)
        ):
            return message
    raise ValueError("child_media_ref_unauthorized")


def resolve_child_user_input(
    parent: RunSnapshot,
    media_refs: tuple[ChildMediaRef, ...],
    *,
    child_profile: ModelProfile,
    task: str,
) -> NativeUserInput | None:
    """Resolve host-authorized media references into a child ``NativeUserInput``.

    Returns ``None`` for the legacy text-only request so the child run keeps its
    original plain-text goal identity. Raises ``ValueError`` with a ``child_*``
    code on any unauthorized, forged, foreign or unsupported-image case; the
    caller maps that to a failed child run before any model dispatch.
    """
    if not media_refs:
        return None
    if len(media_refs) > _MAX_MEDIA_REFS:
        raise ValueError("child_media_ref_invalid")
    owner = _parent_user_owner_message(parent)
    content = owner.get("content")
    if not isinstance(content, list):
        # A plain-text parent goal carries no addressable image parts. Any
        # reference into it is foreign, not a silent text fallback.
        raise ValueError("child_media_ref_unauthorized")
    images: list[ModelImageContent] = []
    for ref in media_refs:
        if ref.source != PARENT_USER_INPUT_SOURCE:
            raise ValueError("child_media_ref_unauthorized")
        index = ref.index
        if index < 0 or index >= len(content):
            raise ValueError("child_media_ref_not_image")
        block = content[index]
        if not isinstance(block, dict) or block.get("type") != "image_url":
            raise ValueError("child_media_ref_not_image")
        # Preserve the original immutable image identity (data URL, detail).
        images.append(ModelImageContent.model_validate(block))
    # The child goal is the prose task; the image parts follow it as the user
    # content, matching the ordinary product user-input contract.
    user_input = NativeUserInput(
        goal=task,
        content=(ModelTextContent(text=task), *images),
    )
    try:
        user_input.check_profile(child_profile)
    except ValueError:
        # An image-incapable child role model never receives the image; this
        # fails closed before the child kernel starts any model exchange.
        raise ValueError("child_user_image_input_unsupported") from None
    return user_input


__all__ = ["PARENT_USER_INPUT_SOURCE", "resolve_child_user_input"]
