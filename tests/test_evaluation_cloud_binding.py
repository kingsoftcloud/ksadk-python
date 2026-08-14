from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.evaluation.cloud_binding import (
    CloudBinding,
    CloudBindingError,
    CloudBindingStore,
)


def _binding() -> CloudBinding:
    return CloudBinding(
        evalset_path="evaluations/support.yaml",
        content_digest="a" * 64,
        provider="agent-eval/evalsmith",
        project_id="project_001",
        dataset_id="dataset_001",
        dataset_version=3,
        schema_hash="b" * 64,
    )


def test_binding_store_round_trips_atomically_below_workspace(tmp_path: Path):
    store = CloudBindingStore(tmp_path)
    binding = _binding()

    path = store.write(binding)

    assert path.is_file()
    assert path.parent == tmp_path / ".agentkit" / "evaluation-bindings"
    assert store.read(binding.evalset_path) == binding


def test_binding_rejects_credential_material_and_workspace_escape(tmp_path: Path):
    with pytest.raises(ValueError, match="Extra inputs"):
        CloudBinding.model_validate({**_binding().model_dump(), "apiKey": "secret"})

    store = CloudBindingStore(tmp_path)
    escaped = _binding().model_copy(update={"evalset_path": "../support.yaml"})
    with pytest.raises(CloudBindingError, match="工作区"):
        store.write(escaped)

