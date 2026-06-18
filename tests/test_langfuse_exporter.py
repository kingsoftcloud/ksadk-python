from __future__ import annotations

from types import SimpleNamespace

from ksadk.tracing.exporters.langfuse_exporter import LangfuseExporterConfig, _LangfuseSpanExporter


class _FakeLangfuse:
    def __init__(self):
        self.traces: list[dict] = []

    def trace(self, **kwargs):
        self.traces.append(kwargs)
        return SimpleNamespace(generation=lambda **_kwargs: None, span=lambda **_kwargs: None)

    def flush(self):
        return None


class _FakeSpan:
    def __init__(self, attributes: dict[str, object]):
        self.name = "demo-agent"
        self.attributes = attributes
        self.parent = None
        self.context = SimpleNamespace(trace_id=1, span_id=2)


def _exporter_with_fake_client() -> tuple[_LangfuseSpanExporter, _FakeLangfuse]:
    fake = _FakeLangfuse()
    exporter = _LangfuseSpanExporter(
        LangfuseExporterConfig(public_key="pk-test", secret_key="sk-test")
    )
    exporter._langfuse = fake
    exporter._agent_config = None
    return exporter, fake


def test_langfuse_exporter_reads_openinference_style_user_and_session_keys():
    exporter, fake = _exporter_with_fake_client()

    exporter._export_trace(
        "trace-1",
        [
            _FakeSpan(
                {
                    "langfuse.session.id": "conv-a",
                    "session.id": "conv-a",
                    "langfuse.user.id": "user-a",
                    "user.id": "user-a",
                    "user.input": "hello",
                    "agent.output": "hi",
                }
            )
        ],
    )

    assert fake.traces[-1]["session_id"] == "conv-a"
    assert fake.traces[-1]["user_id"] == "user-a"


def test_langfuse_exporter_still_reads_legacy_user_and_session_keys():
    exporter, fake = _exporter_with_fake_client()

    exporter._export_trace(
        "trace-1",
        [
            _FakeSpan(
                {
                    "langfuse.session_id": "legacy-session",
                    "langfuse.user_id": "legacy-user",
                }
            )
        ],
    )

    assert fake.traces[-1]["session_id"] == "legacy-session"
    assert fake.traces[-1]["user_id"] == "legacy-user"
