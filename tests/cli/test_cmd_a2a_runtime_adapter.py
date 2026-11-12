from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ksadk.cli import cmd_a2a


def test_a2a_loads_runtime_adapter_from_detected_project(monkeypatch, tmp_path: Path) -> None:
    detection = SimpleNamespace(
        type=SimpleNamespace(value="langgraph"),
        raw_config={"entrypoint": "agent:graph"},
    )
    adapter = object()
    observed: dict[str, object] = {}
    monkeypatch.setattr(cmd_a2a, "_detect_project", lambda path: detection)
    monkeypatch.setattr(
        cmd_a2a,
        "_setup_tracing",
        lambda runtime_type: observed.update(runtime_type=runtime_type),
    )
    monkeypatch.setattr(
        cmd_a2a,
        "create_runtime_adapter",
        lambda context: observed.update(context=context) or adapter,
    )

    result_detection, result_adapter = cmd_a2a._load_runtime_adapter(
        tmp_path,
        no_trace=False,
    )

    assert (result_detection, result_adapter) == (detection, adapter)
    context = observed["context"]
    assert context.runtime_type == "langgraph"
    assert context.project_dir == tmp_path
    assert context.detection is detection
    assert context.config == {"entrypoint": "agent:graph"}
    assert observed["runtime_type"] == "langgraph"
