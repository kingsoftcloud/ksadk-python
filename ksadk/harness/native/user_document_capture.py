"""Persist admitted source text before granting its reference in the start fact."""

import hashlib

from .ports import ArtifactPort, ArtifactRef, ArtifactWriteRequest
from .state import RunScope
from .user_input import NativeUserInput


async def capture_user_documents(
    value: NativeUserInput,
    *,
    artifacts: ArtifactPort | None,
    scope: RunScope,
    run_id: str,
) -> NativeUserInput:
    documents = []
    for index, document in enumerate(value.documents):
        if document.full_text is None:
            documents.append(document)
            continue
        if artifacts is None:
            raise ValueError("native_user_document_storage_unavailable")
        text = document.full_text
        prefix = document.text.removesuffix("\n...[内容已截断]")
        if not text.startswith(prefix) or document.extracted_text_chars != len(text):
            raise ValueError("native_user_document_capture_mismatch")
        ref = ArtifactRef.model_validate(
            await artifacts.write(
                ArtifactWriteRequest(
                    scope=scope,
                    run_id=run_id,
                    operation_key=f"user-document/{index}/{document.source_digest}",
                    tool_call_id=f"input-document-{index}",
                    tool_name="user_document_input",
                    content=text,
                    mime_type="text/plain",
                )
            )
        )
        encoded = text.encode("utf-8")
        if (
            ref.content_hash != "sha256:" + hashlib.sha256(encoded).hexdigest()
            or ref.size_bytes != len(encoded)
            or ref.mime_type != "text/plain"
        ):
            raise ValueError("native_user_document_capture_mismatch")
        documents.append(document.model_copy(update={"full_text": None, "text_artifact": ref}))
    return NativeUserInput.model_validate(value.model_copy(update={"documents": tuple(documents)}))
