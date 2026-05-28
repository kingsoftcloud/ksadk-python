# Agent Context

This guide explains what context reaches business agent code when KsADK invokes
ADK, LangGraph, LangChain, or DeepAgents projects.

## Recommended Pattern

Do not make business code parse raw UI events or internal session records. Use a
framework hook or runner payload boundary:

| Framework | Recommended integration |
| --- | --- |
| LangGraph | define `ksadk_prepare_state(payload, session_context)` |
| LangChain | define `ksadk_prepare_input(payload, session_context)` |
| ADK | consume ADK message/session primitives produced by the runner |
| DeepAgents | configure the model/tools and consume normalized input |

For LangGraph and LangChain, the hook must be visible from the configured
`entry_point` module.

## Standard Payload Fields

The runner payload can include:

| Field | Meaning |
| --- | --- |
| `input` | current user input text or resume payload |
| `history` | projected conversation history |
| `input_content` | current input as Responses-style content blocks |
| `input_messages` | current input as Responses-style messages/items |
| `input_parts` | legacy/internal normalized parts |
| `attachments` | effective attachment context, possibly restored from session |
| `attachment_results` | effective extracted text/OCR/document results |
| `current_attachments` | attachments from the current user turn only |
| `current_attachment_results` | extraction results from the current user turn only |
| `has_current_files` | whether this user turn included files/images |
| `model` | per-request model override |
| `model_metadata` | model capability metadata where available |
| `instructions` | request-level system/developer instruction |

Session context can include:

| Field | Meaning |
| --- | --- |
| `history` | current conversation history |
| `platform_context` | agent ID, user ID, session ID, and related runtime identity |
| `kb_context` | knowledge-base retrieval context |
| `memory_context` | long-term memory context |
| `is_resume` | whether the request is resuming an interrupted run |

## LangGraph Hook

```python
def ksadk_prepare_state(payload: dict, session_context: dict) -> dict:
    if session_context.get("is_resume"):
        return payload.get("input")

    return {
        "query": payload["input"],
        "history": session_context.get("history", []),
        "attachments": payload.get("attachments", []),
        "attachment_results": payload.get("attachment_results", []),
        "current_attachments": payload.get("current_attachments", []),
        "current_attachment_results": payload.get("current_attachment_results", []),
        "has_current_files": payload.get("has_current_files", False),
        "input_content": payload.get("input_content", []),
        "input_messages": payload.get("input_messages", []),
        "platform_context": session_context.get("platform_context"),
        "kb_context": session_context.get("kb_context"),
        "memory_context": session_context.get("memory_context"),
        "model_metadata": payload.get("model_metadata", {}),
    }
```

When `is_resume` is true, return the resume payload directly. Do not wrap it as
a new graph state unless your graph explicitly expects that.

## LangChain Hook

```python
def ksadk_prepare_input(payload: dict, session_context: dict) -> dict:
    return {
        "question": payload["input"],
        "history": session_context.get("history", []),
        "attachment_texts": [
            item.get("text", "")
            for item in payload.get("attachment_results", [])
            if isinstance(item, dict) and item.get("text")
        ],
        "input_content": payload.get("input_content", []),
        "input_messages": payload.get("input_messages", []),
        "model_metadata": payload.get("model_metadata", {}),
    }
```

Your chain decides how to map this dictionary into prompts, tools, retrievers, or
model-native multimodal inputs.

## Current Turn Versus Session Context

Use current-turn fields when the answer should depend on files the user just
uploaded:

```python
if payload.get("has_current_files"):
    results = payload.get("current_attachment_results", [])
```

Use effective context fields when the user is following up on an earlier file in
the same session:

```python
results = payload.get("attachment_results", [])
```

This distinction matters for workflows such as "summarize this PDF" followed by
"expand the second risk".

## Model Metadata

`model_metadata` can describe input modalities and capabilities. A common check:

```python
supports_image = bool(
    ((model_metadata or {}).get("capabilities") or {}).get("multimodal_input_image")
)
```

If a model does not support native image input, prefer extracted OCR/document
text from `attachment_results`.

## Runtime Context Helper

Shared tools and helper functions can read the active invocation context:

```python
from ksadk.runtime_context import get_current_invocation_context

ctx = get_current_invocation_context()
if ctx:
    print(ctx.agent_id)
    print(ctx.user_id)
    print(ctx.session_id)
    print(ctx.input_content)
    print(ctx.current_attachments)
```

Use this for cross-cutting helpers. Business graph state should still prefer
explicit hook inputs because they are easier to test.

## What Not To Do

Avoid:

- reading private event-store internals from business code.
- assuming `attachments` means "files uploaded in this turn".
- treating `inlineData` and `fileData` as official OpenAI fields.
- mixing hosted gateway headers into local examples.
- storing secrets in graph state, logs, traces, or public fixtures.
