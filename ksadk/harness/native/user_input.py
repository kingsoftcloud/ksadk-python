"""One typed user turn, with the textual goal and actual model content together."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model import ModelImageContent, ModelProfile, ModelTextContent
from .user_document import NativeUserDocument


def user_content_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(
        not isinstance(part, dict) or part.get("type") not in {"text", "image_url"}
        for part in content
    ):
        return None
    texts = [part.get("text") for part in content if part.get("type") == "text"]
    if any(not isinstance(text, str) for text in texts):
        return None
    return "\n".join(texts).strip()


def matches_user_goal(content: object, goal: str) -> bool:
    # Preserve the exact identity of the original plain-text contract.
    return (
        content == goal if isinstance(content, str) else user_content_text(content) == goal.strip()
    )


class NativeUserInput(BaseModel):
    """Host-normalized content; arbitrary message roles/metadata are not admitted."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    version: Literal["native-user-parts/v1"] = "native-user-parts/v1"
    goal: str = Field(strict=True, min_length=1, max_length=1_048_576)
    content: tuple[ModelTextContent | ModelImageContent, ...] = Field(min_length=1, max_length=64)
    documents: tuple[NativeUserDocument, ...] = Field(
        default=(), max_length=8, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def bind_goal(self):
        if (
            not self.goal.strip()
            or user_content_text(self.message()["content"]) != self.goal.strip()
        ):
            raise ValueError("native_user_input_goal_mismatch")
        return self

    def message(self) -> dict:
        return {
            "role": "user", "content": [part.model_dump(mode="json") for part in self.content],
            **({"user_document_sources": [item.descriptor() for item in self.documents]}
               if self.documents else {}),
        }

    def messages(self) -> tuple[dict, ...]:
        owner = self.message()
        return (owner, *(item.message(owner) for item in self.documents))

    def check_profile(self, profile: ModelProfile) -> None:
        if profile.image_input_mode == "text_only" and any(
            isinstance(part, ModelImageContent) for part in self.content
        ):
            raise ValueError("native_user_image_input_unsupported")


def native_input_goal(value: str | NativeUserInput) -> str:
    return value.goal if isinstance(value, NativeUserInput) else value


def current_user_has_media(messages, goal: str) -> bool:
    for message in reversed(messages):
        if (
            message.get("role") == "user"
            and message.get("kind") is None
            and matches_user_goal(message.get("content"), goal)
        ):
            content = message.get("content")
            return bool(message.get("user_document_sources")) or isinstance(content, list) and any(
                part.get("type") == "image_url" for part in content
            )
    return False
