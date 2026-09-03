"""Studio Runtime 目录必须反映 Adapter 真实能力声明。"""

from __future__ import annotations

from ksadk.studio.runtime_catalog import inspect_runtime_catalog


class _Executor:
    def registered_runtime_types(self) -> tuple[str, ...]:
        return ("harness", "codex")


def test_harness_capabilities_reflect_managed_adapter_declaration() -> None:
    items = {item["runtimeType"]: item for item in inspect_runtime_catalog(_Executor())}

    harness = items["harness"]["capabilities"]
    assert harness["resume"] != "not_supported"
    assert harness["checkpoint"] != "not_supported"
    # 进程内恢复为真实声明；跨进程持久性取决于装配档位，如实标注
    assert harness["durableAcrossProcess"] is False
    assert harness["checkpointGranularity"] == "snapshot"


def test_external_runtime_static_declarations_unchanged() -> None:
    items = {item["runtimeType"]: item for item in inspect_runtime_catalog(_Executor())}

    codex = items["codex"]["capabilities"]
    assert codex["resume"] == "thread_id"
    assert codex["checkpoint"] == "thread"
