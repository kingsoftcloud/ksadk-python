"""Bind Studio's authorized input parts to the display goal before Native start."""

from pydantic import ValidationError

from ksadk.harness.native.model import ModelImageContent, ModelTextContent
from ksadk.harness.native.user_input import NativeUserInput, user_content_text
from ksadk.studio.native_document_input import native_document_input


def admit_native_user_input(raw, goal: str) -> str | NativeUserInput:
    if isinstance(raw, str):
        if raw != goal:
            raise ValueError("native_studio_runtime_input_unbound")
        return raw
    if not isinstance(raw, list) or not raw or len(raw) > 64:
        raise ValueError("native_user_input_invalid")
    content = []
    documents = []
    try:
        for part in raw:
            if not isinstance(part, dict):
                raise ValueError("native_user_input_invalid")
            if part.get("type") == "text":
                content.append(ModelTextContent.model_validate(part))
            elif part.get("type") == "image" and set(part) == {"type", "url"}:
                content.append(
                    ModelImageContent.model_validate(
                        {
                            "type": "image_url",
                            "image_url": {"url": part["url"]},
                        }
                    )
                )
            elif part.get("type") == "image_url":
                content.append(ModelImageContent.model_validate(part))
            elif part.get("type") == "input_file":
                if len(documents) >= 8:
                    raise ValueError("native_user_document_count_exceeded")
                documents.append(native_document_input(part))
            else:
                raise ValueError("native_user_input_part_unsupported")
    except ValidationError:
        raise ValueError("native_user_input_invalid") from None
    if any(
        isinstance(part, ModelImageContent) and not part.image_url.url.startswith("data:")
        for part in content
    ):
        # Ordinary Studio attachments are resolved and verified by the product
        # before this admission. A remote URL is not an uploaded attachment.
        raise ValueError("native_user_input_image_source_unsupported")
    text = user_content_text([part.model_dump(mode="json") for part in content])
    if not text:
        # The attachment-only composer supplies a descriptive display goal.
        content.insert(0, ModelTextContent(text=goal))
    elif text != goal.strip():
        raise ValueError("native_studio_runtime_input_unbound")
    try:
        return NativeUserInput(goal=goal, content=tuple(content), documents=tuple(documents))
    except ValidationError:
        raise ValueError("native_user_input_invalid") from None
