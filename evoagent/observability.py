"""Tracing helpers that degrade cleanly when OpenTelemetry is not installed."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .errors import safe_exception_fields
from .ports import AlertStorePort

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
logger = logging.getLogger("evoagent")


class Observability:
    def __init__(self, service_name: str = "evoagent", endpoint: str = ""):
        self.tracer = None
        self._provider: Any = None
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            self._provider = provider
            if endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            self.tracer = provider.get_tracer(service_name)
        except ImportError:
            self.close()
            self.tracer = None

    def close(self) -> None:
        provider, self._provider = self._provider, None
        if provider is not None:
            provider.shutdown()

    @contextmanager
    def span(self, name: str, trace_id: str = "", **attributes):
        token = trace_id_var.set(trace_id or trace_id_var.get())
        if self.tracer:
            with self.tracer.start_as_current_span(
                name,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                for key, value in attributes.items():
                    if value is not None:
                        span.set_attribute(key, value)
                try:
                    yield span
                except Exception as exc:
                    fields = safe_exception_fields(exc)
                    span.set_attribute("error.type", fields["error_type"])
                    span.set_attribute("error.ref", fields["error_ref"])
                    span.add_event("evoagent.failure", fields)
                    raise
                finally:
                    trace_id_var.reset(token)
        else:
            try:
                yield None
            finally:
                trace_id_var.reset(token)


class AlertManager:
    def __init__(self, store: AlertStorePort, failure_rate: float = 0.2, min_samples: int = 10):
        self.store = store
        self.failure_rate = failure_rate
        self.min_samples = min_samples

    def evaluate(self, tenant_id: str) -> None:
        stats = self.store.dashboard_stats(tenant_id)
        samples = stats["tasks_success"] + stats["tasks_failed"]
        if samples >= self.min_samples:
            rate = stats["tasks_failed"] / samples
            if rate > self.failure_rate:
                self.store.create_alert(
                    tenant_id,
                    "review-failure-rate",
                    "critical",
                    "Review failure rate %.1f%% exceeds the %.1f%% threshold."
                    % (rate * 100, self.failure_rate * 100),
                )
            else:
                self.store.clear_alert(tenant_id, "review-failure-rate")
