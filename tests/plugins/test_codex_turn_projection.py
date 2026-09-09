"""Provider input policy crosses one typed seam into the native lifecycle."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ksadk.codex.projection import ProjectedCodexTurn
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.plugins.providers.codex_turn import CodexTurnProjector
from ksadk.runtime.adapter import StartRequest
from tests.runners.test_adapter_contract import _FakeCodexClient

pytest.importorskip("openai_codex")


def request(**values):
    return StartRequest(input=values.pop("input", "current turn"), user_id="user-a",
                        session_id="session-a", **values)


def test_projection_preserves_native_parts_and_does_not_repeat_resumed_history():
    projector = CodexTurnProjector(bound_skill_paths={"guide": "/locked/guide/SKILL.md"})
    original = request(
        input=[{"type": "text", "text": "current turn"},
               {"type": "image", "url": "https://example.invalid/image.png"}],
        config={"skills": [{"name": "guide", "path": "/draft/guide/SKILL.md"}]},
        metadata={"conversation_request": {"messages": [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "current turn"},
        ]}},
    )
    try:
        admitted = projector.prepare(original)
        fresh = projector.project(admitted, None).input
        assert fresh[0].path == "/locked/guide/SKILL.md"
        assert "previous question" in fresh[1].text
        assert fresh[2].url == "https://example.invalid/image.png"
        assert original.config["skills"][0]["path"] == "/draft/guide/SKILL.md"
        resumed = admitted.model_copy(update={"metadata": {**admitted.metadata, "thread_id": "t"}})
        native = projector.project(resumed, None).input
        assert native[1].text == "current turn"
        assert native[2].url == fresh[2].url
    finally:
        projector.close()


def attachment_request():
    return request(input=[{
        "type": "input_file", "filename": "notes.txt",
        "file_data": "data:text/plain;base64," + base64.b64encode(b"private evidence").decode(),
    }])


@pytest.mark.asyncio
async def test_attachment_paths_survive_adapter_close_for_native_thread_continuation():
    first, other = CodexTurnProjector(), CodexTurnProjector()
    first_input = first.project(attachment_request(), None).input
    other_input = other.project(attachment_request(), None).input
    first_path, other_path = Path(first_input[1].path), Path(other_input[1].path)
    assert first_path == other_path
    assert "private evidence" in first_input[0].text
    assert first_path.read_bytes() == other_path.read_bytes()

    class ClosingClient(_FakeCodexClient):
        async def close(self):
            assert first_path.is_file(), "file was removed while App Server could still read it"

    adapter = CodexRuntimeAdapter(ClosingClient(), turn_projector=first)
    await adapter.close_all()
    await adapter.close_all()
    assert first_path.is_file()
    assert other_path.is_file()
    other.close()
    assert other_path.is_file()
    with pytest.raises(RuntimeError, match="projector is closed"):
        first.project(request(), None)


@pytest.mark.asyncio
async def test_runtime_consumes_injected_projection_without_own_input_policy():
    seen = []

    class Projector:
        def prepare(self, incoming):
            return incoming.model_copy(update={"config": {"cwd": "/admitted"}})

        def project(self, incoming, payload):
            assert incoming.config["cwd"] == "/admitted"
            return ProjectedCodexTurn("provider projected input")

        def close(self):
            seen.append("projection closed")

    class Client(_FakeCodexClient):
        def run_turn(self, thread_id, prompt, *, config=None):
            seen.append(prompt)
            return super().run_turn(thread_id, prompt, config=config)

    adapter = CodexRuntimeAdapter(
        Client(block=False, with_approval=False), turn_projector=Projector(),
    )
    handle = await adapter.start(request())
    try:
        assert [event async for event in adapter.stream(handle)]
        assert seen == ["provider projected input"]
    finally:
        await adapter.close_all()
    assert seen[-1] == "projection closed"


def test_provider_projection_rejects_catalog_name_drift_and_pins_all_skills():
    projector = CodexTurnProjector(
        bound_skill_paths={"guide": "/locked/guide/SKILL.md"}, enforce_bound_skills=True,
    )
    admitted = projector.prepare(request(config={"skills": []}))
    assert admitted.config["skills"] == [{"name": "guide", "path": "/locked/guide/SKILL.md"}]
    with pytest.raises(ValueError, match="immutable Bundle"):
        projector.prepare(request(config={"skills": [{"name": "changed", "path": "/mutable"}]}))
    empty = CodexTurnProjector(enforce_bound_skills=True)
    with pytest.raises(ValueError, match="immutable Bundle"):
        empty.prepare(request(config={"skills": [{"name": "extra", "path": "/mutable"}]}))
