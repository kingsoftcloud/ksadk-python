# Observability And Tracing

KsADK includes local spans, a Langfuse-compatible path, and a standard OTLP HTTP
traces exporter. Tracing is optional diagnostics and should not be required for
the quickstart path.

Starting in `0.6.2`, the recommended model is **OTel-first**: application code
creates OpenTelemetry spans, span events, and attributes, while
`OTEL_EXPORTER_OTLP_*` environment variables route data to Langfuse, an OTel
Collector, or another compatible backend.

## Export Paths

| Path | Default behavior | Purpose |
| --- | --- | --- |
| In-memory spans | enabled for local runner paths | local debug APIs and Web UI trace views |
| Generic OTLP HTTP | enabled when `OTEL_EXPORTER_OTLP_*` is set | send spans to Langfuse or any OTLP HTTP Collector |
| Langfuse OTLP HTTP | enabled when no generic OTLP exists and Langfuse keys are set | compatibility with older Langfuse env vars |
| OTLP gRPC | explicit `enable_otlp=True` | compatibility with older code paths |
| OpenInference instrumentation | best effort | framework spans for ADK, LangChain, and similar runtimes |

The local in-memory exporter is the safest public default. Remote exporters
should be enabled through environment variables. Local runs should continue if
an external exporter is missing or fails to initialize.

## Generic OTLP HTTP

When only the generic endpoint is set, KsADK derives `/v1/traces`:

```bash
export OTEL_SERVICE_NAME="customer-agent-runtime"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otel-collector.example.com/otel"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20demo-token"

agentengine run .
```

You can also configure traces-specific endpoint, protocol, and headers.
`TRACES_*` values take precedence over generic values:

```bash
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://otel-collector.example.com/otel/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer%20trace-token"
```

Headers follow the OTLP comma-separated format. URL-encode header values; KsADK
decodes `Bearer%20trace-token` to `Bearer trace-token`.

## Langfuse Compatibility

If a project still uses Langfuse environment variables, the existing setup
continues to work:

```bash
export LANGFUSE_PUBLIC_KEY="pk-example"
export LANGFUSE_SECRET_KEY="secret-example"
export LANGFUSE_BASE_URL="https://langfuse.example.com"

agentengine run .
```

When generic OTLP and Langfuse variables are both present,
`setup_tracing(enable_langfuse=None)` prefers generic OTLP and does not enable a
second Langfuse direct exporter. This avoids duplicate traces in Langfuse.
Explicit `setup_tracing(enable_langfuse=True)` can still force the Langfuse
compatibility path.

To use Langfuse as an OTLP backend, configure standard OTLP variables:

```bash
export OTEL_SERVICE_NAME="customer-agent-runtime"
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://langfuse.example.com/api/public/otel/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic%20<base64-public-secret>,x-langfuse-ingestion-version=4"
```

Public examples must use placeholder keys and user-owned endpoints. Do not
publish real trace screenshots, tenant ids, or private URLs.

## Callback-only Mode

LangChain or LangGraph projects may prefer the Langfuse callback handler instead
of direct OTLP. Enable callback-only mode explicitly:

```bash
export LANGFUSE_PUBLIC_KEY="pk-example"
export LANGFUSE_SECRET_KEY="secret-example"
export LANGFUSE_BASE_URL="https://langfuse.example.com"
export LANGFUSE_USE_CALLBACK="true"

agentengine run .
```

Choose either callback or direct OTLP for a run unless duplicate traces have
been reviewed and accepted.

## Span Events And Child Spans

Span events are timestamped records attached to a span. They work well for:

- `checkpoint.saved`
- `agent.run.started`
- `analysis.milestone`
- error hints and resume hints

Child spans are independent steps in the trace tree. They work well for:

- tool calls.
- checkpoint persistence.
- external I/O.
- report generation.
- score calculation.

In Langfuse and similar backends, child spans are usually easier to see in the
trace tree or observation list and are better for duration, status, tool name,
and error aggregation. Span events usually appear inside the parent span details
and may not become independent tree nodes.

## Metadata And Scores

Long-running, multi-user, multi-instance systems should attach stable,
non-sensitive attributes to important spans:

- `ksadk.agent_id`
- `ksadk.session_id`
- `ksadk.user_id`
- `ksadk.invocation_id`
- `ksadk.runtime.service`
- `ksadk.runtime.instance_id`

Represent evaluation scores with backend-neutral attributes first:

```python
span.set_attribute("score.name", "answer_quality")
span.set_attribute("score.value", 0.88)
span.set_attribute("score.source", "auto_evaluator")
span.set_attribute("score.comment", "Report covers evidence and next steps.")
```

If the backend is Langfuse, a platform service or OTel Collector can map
`score.*` attributes to native Langfuse scores. Application code does not need
to import the Langfuse SDK, which keeps backend replacement cheaper.

Do not put raw prompts, credentials, private URLs, customer names, or uploaded
file contents into span attributes.

## Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| Local trace view is empty | no run has produced spans, or tracing was disabled | run one request and confirm local tracing is enabled |
| Generic OTLP receives no data | endpoint, TLS, auth, or Collector policy mismatch | check `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and headers |
| Langfuse receives no spans | missing key, wrong base URL, callback-only mode, or generic OTLP precedence | check Langfuse vars, `LANGFUSE_USE_CALLBACK`, and `OTEL_EXPORTER_OTLP_*` |
| Duplicate Langfuse traces | callback and direct OTLP are both enabled | choose one export path for the project |
| Score is not shown as a native score | the backend does not map `score.*` attributes | add conversion in the Collector or platform service |
| Framework spans are sparse | optional instrumentation is missing or inactive | install tracing extras and check framework instrumentation |

## Public Documentation Rules

- use placeholder keys and user-owned endpoints.
- never commit `.env` files containing tracing credentials.
- never publish private trace ids, tenant ids, customer names, or real screenshots.
- keep private Collector URLs and tenant routing in internal runbooks, not in the public SDK repository.
