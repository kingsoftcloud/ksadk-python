"""Explicit, bounded request-scoped OTLP routing for trusted service entrypoints."""

import logging
import math
import re
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Condition, Event, Thread
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from weakref import finalize

import requests
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import SpanExportResult

_BACKENDS = ("langfuse", "cloud_monitor")
_exporting: ContextVar[bool] = ContextVar("ksadk_routed_exporting", default=False)


class _TransportLogFilter(logging.Filter):
    def filter(self, record):
        return not _exporting.get()


_transport_log_filter = _TransportLogFilter()


class _RoutedSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Never inherit netrc or a process-global credential provider's identity.
        self.trust_env = False

    def post(self, url, **kwargs):
        kwargs["allow_redirects"] = False
        kwargs["cert"] = None
        response = super().post(url, **kwargs)
        if 300 <= response.status_code < 400:
            response.close()
            raise requests.RequestException("Observability redirects are not allowed")
        return response


@dataclass(frozen=True)
class _Target:
    backend: str
    binding_id: str
    binding_version: str
    endpoint: str = field(repr=False)
    protocol: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    credential_version: str


@dataclass(frozen=True)
class _Route:
    route_id: str
    project_id: str
    account_id: str
    experiment_id: str
    run_id: str
    targets: tuple[_Target, ...] = field(repr=False)


_route: ContextVar[_Route | None] = ContextVar("ksadk_observability_route", default=None)


def _identifier(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"Invalid observability {name}")
    return str(value)


def _parse_route(config: Mapping[str, Any]) -> _Route:
    if not isinstance(config, Mapping):
        raise ValueError("Observability route must be a mapping")
    identifiers = {
        k: _identifier(config, k)
        for k in ("route_id", "project_id", "account_id", "experiment_id", "run_id")
    }
    raw_targets = config.get("targets")
    if not isinstance(raw_targets, (list, tuple)) or len(raw_targets) > 2:
        raise ValueError("Observability route requires zero to two targets")
    targets = []
    seen = set()
    for raw in raw_targets:
        if not isinstance(raw, Mapping):
            raise ValueError("Invalid observability target")
        backend = raw.get("backend")
        if backend not in _BACKENDS or backend in seen:
            raise ValueError("Invalid or duplicate observability backend")
        seen.add(backend)
        endpoint = raw.get("endpoint")
        try:
            url = urlsplit(endpoint) if isinstance(endpoint, str) else None
            valid_url = url and url.scheme in ("http", "https") and url.hostname
            if (
                not valid_url
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or any(c.isspace() for c in endpoint)
            ):
                raise ValueError
            url.port
        except ValueError:
            raise ValueError("Invalid observability endpoint") from None
        if raw.get("protocol") != "http/protobuf":
            raise ValueError("Observability protocol must be http/protobuf")
        headers = raw.get("headers")
        if not isinstance(headers, Mapping) or not headers:
            raise ValueError("Observability headers are required")
        names = set()
        for key, value in headers.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value
                or "\r" in key + value
                or "\n" in key + value
                or key.lower() in names
            ):
                raise ValueError("Invalid observability headers")
            try:
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                    raise ValueError
                requests.utils.check_header_validity((key, value))
                value.encode("latin-1")
            except (ValueError, requests.RequestException):
                raise ValueError("Invalid observability headers") from None
            names.add(key.lower())
        targets.append(
            _Target(
                backend=backend,
                binding_id=_identifier(raw, "binding_id"),
                binding_version=_identifier(raw, "binding_version"),
                endpoint=endpoint,
                protocol=raw["protocol"],
                headers=tuple(sorted(headers.items())),
                credential_version=_identifier(raw, "credential_version"),
            )
        )
    return _Route(**identifiers, targets=tuple(targets))


@contextmanager
def bind_observability_route(config: Mapping[str, Any]) -> Iterator[None]:
    """Bind a validated, copied route; callers must authorize its provenance first.

    Async tasks and asyncio.to_thread inherit the context. For raw thread pools,
    submit contextvars.copy_context().run; no global/thread fallback is used.
    """
    from ksadk.tracing import setup

    if setup._routed_processor is None or setup._routed_processor.closed:
        raise RuntimeError("Routed tracing must be initialized before binding a route")
    token = _route.set(_parse_route(config))
    try:
        yield
    finally:
        _route.reset(token)


def _routed_resource(resource: Resource) -> Resource:
    """Keep stable service metadata, never a previous tenant's resource binding."""
    return Resource(
        {
            key: value
            for key, value in resource.attributes.items()
            if not key.startswith(("agentengine.", "evalsmith.", "langfuse.", "cloud_monitor."))
        },
        schema_url=resource.schema_url,
    )


class RoutedSpanProcessor(SpanProcessor):
    """Two independent bounded workers; credentials stay in private route snapshots."""

    def __init__(
        self,
        *,
        max_queue_size=512,
        max_active_spans=4096,
        max_cached_exporters=64,
        cache_ttl_seconds=300,
        export_timeout_seconds=10,
    ):
        limits = (max_queue_size, max_active_spans, max_cached_exporters)
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in limits):
            raise ValueError("Routed tracing capacities must be positive integers")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in (cache_ttl_seconds, export_timeout_seconds)
        ):
            raise ValueError("Routed tracing timeouts must be positive")
        for name in (
            "opentelemetry.exporter.otlp.proto.http.trace_exporter",
            "opentelemetry.exporter.otlp.proto.http",
            "urllib3.connectionpool",
            "urllib3.util.retry",
        ):
            logging.getLogger(name).addFilter(_transport_log_filter)
        self._max_active_spans = max_active_spans
        self._max_cached_exporters = max_cached_exporters
        self._cache_ttl = cache_ttl_seconds
        self._export_timeout = export_timeout_seconds
        self._condition = Condition()
        self._active = {}
        self._queues = {b: Queue(maxsize=max_queue_size) for b in _BACKENDS}
        self._stop = Event()
        self.closed = False
        self._status = {
            b: dict(
                submitted=0,
                completed=0,
                exported=0,
                failed=0,
                dropped=0,
                last_result=None,
                last_route_id=None,
            )
            for b in _BACKENDS
        }
        self._threads = [
            Thread(target=self._work, args=(b,), daemon=True, name=f"ksadk-otlp-{b}")
            for b in _BACKENDS
        ]
        for thread in self._threads:
            thread.start()

    @property
    def status(self):
        """Return bounded, credential-free per-backend delivery counters."""
        with self._condition:
            return {b: dict(s) for b, s in self._status.items()}

    @staticmethod
    def _route_attributes(route):
        return {
            "agentengine.account_id": route.account_id,
            "evalsmith.project_id": route.project_id,
            "evalsmith.experiment_id": route.experiment_id,
            "evalsmith.run_id": route.run_id,
            "evalsmith.route_id": route.route_id,
        }

    def on_start(self, span, parent_context=None):
        route = _route.get()
        if route is None:
            return
        span.set_attributes(self._route_attributes(route))
        key = (span.context.trace_id, span.context.span_id)
        with self._condition:
            if self.closed or len(self._active) >= self._max_active_spans:
                for target in route.targets:
                    self._status[target.backend]["dropped"] += 1
                return
            cleanup = finalize(span, self._forget, key)
            self._active[key] = (route, cleanup)

    def _forget(self, key):
        with self._condition:
            entry = self._active.pop(key, None)
            if entry is not None:
                for target in entry[0].targets:
                    self._status[target.backend]["dropped"] += 1

    def on_end(self, span):
        key = (span.context.trace_id, span.context.span_id)
        with self._condition:
            entry = self._active.pop(key, None)
            if entry is None:
                return
            route, cleanup = entry
            cleanup.detach()
            from ksadk.tracing.setup import _clone_span

            attributes = dict(span.attributes or {})
            attributes.update(self._route_attributes(route))
            span = _clone_span(
                span, attributes=attributes, resource=_routed_resource(span.resource)
            )
            for target in route.targets:
                state = self._status[target.backend]
                if self.closed:
                    state["dropped"] += 1
                    continue
                try:
                    self._queues[target.backend].put_nowait((route, target, span))
                    state["submitted"] += 1
                except Full:
                    state["dropped"] += 1
                    state["last_result"] = "queue_full"

    def _work(self, backend):
        _exporting.set(True)
        queue = self._queues[backend]
        cache = OrderedDict()
        try:
            while not self._stop.is_set() or not queue.empty():
                try:
                    batch = [queue.get(timeout=0.1)]
                except Empty:
                    self._expire(cache)
                    continue
                for _ in range(63):
                    try:
                        batch.append(queue.get_nowait())
                    except Empty:
                        break
                self._export_batch(backend, batch, cache)
                # Drop snapshots after delivery rather than retaining the last batch while idle.
                del batch
        finally:
            for exporter, _ in cache.values():
                self._close(exporter)

    def _export_batch(self, backend, batch, cache):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        from ksadk.tracing.setup import _prepare_cloud_monitor_spans

        groups = {}
        for route, target, span in batch:
            groups.setdefault((route, target), []).append(span)
        for (route, target), spans in groups.items():
            self._expire(cache)
            key = (route.account_id, route.project_id, target)
            result = SpanExportResult.FAILURE
            try:
                if key not in cache:
                    if len(cache) >= self._max_cached_exporters:
                        _, (old, _) = cache.popitem(last=False)
                        self._close(old)
                    cache[key] = (
                        OTLPSpanExporter(
                            endpoint=target.endpoint,
                            headers=dict(target.headers),
                            timeout=self._export_timeout,
                            session=_RoutedSession(),
                        ),
                        monotonic(),
                    )
                exporter, _ = cache.pop(key)
                cache[key] = (exporter, monotonic())
                payload = (
                    _prepare_cloud_monitor_spans(spans) if backend == "cloud_monitor" else spans
                )
                # OTLP already retries within its timeout; do not multiply that budget.
                result = exporter.export(payload)
            except Exception:
                # Exceptions may embed credentials; retain only a stable failure code.
                pass
            with self._condition:
                state = self._status[backend]
                succeeded = result == SpanExportResult.SUCCESS
                state["exported" if succeeded else "failed"] += len(spans)
                state["completed"] += len(spans)
                state["last_result"] = "success" if succeeded else "export_failed"
                state["last_route_id"] = route.route_id
                self._condition.notify_all()
            for _ in spans:
                self._queues[backend].task_done()

    @staticmethod
    def _close(exporter):
        try:
            exporter.shutdown()
        except Exception:
            pass

    def _expire(self, cache):
        now = monotonic()
        for key, (exporter, used) in list(cache.items()):
            if now - used >= self._cache_ttl:
                del cache[key]
                self._close(exporter)

    def force_flush(self, timeout_millis=30000):
        """Wait for queues to drain; True means drained, not delivered."""
        deadline = monotonic() + timeout_millis / 1000
        with self._condition:
            while any(s["completed"] < s["submitted"] for s in self._status.values()):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def shutdown(self):
        with self._condition:
            if self.closed:
                return
            self.closed = True
            for route, cleanup in self._active.values():
                cleanup.detach()
                for target in route.targets:
                    self._status[target.backend]["dropped"] += 1
            self._active.clear()
            self._stop.set()
        deadline = monotonic() + 30
        for thread in self._threads:
            thread.join(max(0, deadline - monotonic()))
