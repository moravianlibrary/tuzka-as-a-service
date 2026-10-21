"""OpenTelemetry: traces and logs, with one trace per job across processes.

Off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; everything else comes from the
standard ``OTEL_*`` env vars (service name, headers, resource attributes). The SDK is
imported only when it's on, so a deployment without a collector pays nothing, and a
checkout whose environment lacks the packages still runs (every helper is a no-op).

A job's trace starts at submit: the API stores the W3C ``traceparent`` in the job's
Redis hash, and the dispatcher and poller open their spans as its children, so
submit → dispatch → engine polls → harvest is one trace although three processes
handle the job.

Only that is traced. The workers' own loops (a Redis command or SQL query per backend
every tick, health checks) would otherwise each start a one-span trace, ~0.7M + 0.18M
per engine a day, so workers record a span only inside a job's trace, and the API
skips its probe and autoscaler endpoints.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

try:
    from opentelemetry import propagate, trace
except ImportError:  # the packages aren't installed: telemetry stays off
    propagate = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_enabled = False


# API paths not traced: kubelet probes and KEDA's /load poll, every few seconds each.
# Regexes searched in the full URL.
_UNTRACED_URLS = r"/healthz$,/load(\?|$)"


def setup_telemetry(
    default_service: str, db_engine: AsyncEngine | None = None, root_spans: bool = False
) -> bool:
    """Export traces and logs over OTLP and instrument httpx, redis and ``db_engine``.

    ``root_spans`` lets the process start traces of its own (the API: one per request).
    Without it (the workers), a span is recorded only under a parent, i.e. inside a
    job's ``job_span``; everything else is dropped at creation. Logs are exported either
    way. ``OTEL_SERVICE_NAME`` wins over ``default_service``. Call it once per process,
    after logging is configured (it adds a handler to the root logger). Returns whether
    telemetry is on.
    """
    global _enabled
    if _enabled:
        return True
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or trace is None:
        return False

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased

    # Attributes passed here override the env-detected ones, so resolve the env first.
    service = os.environ.get("OTEL_SERVICE_NAME") or default_service
    resource = Resource.create({"service.name": service})

    sampler = ParentBased(root=ALWAYS_ON if root_spans else ALWAYS_OFF)
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    logging.getLogger().addHandler(
        LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    )

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    if db_engine is not None:
        # The async engine wraps a sync one; that's where SQLAlchemy emits its events.
        SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)

    _enabled = True
    logger.info("OpenTelemetry on: service %s", service)
    return True


def instrument_app(app: FastAPI) -> None:
    """Trace the API's requests. Separate from setup_telemetry because only the api
    image has FastAPI (and its instrumentation, in the ``api`` extra)."""
    if not _enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=_UNTRACED_URLS)


def inject_trace_context() -> dict[str, str]:
    """The current trace context as W3C headers (``traceparent``, maybe
    ``tracestate``), to store with a job; empty when nothing is being traced."""
    if propagate is None:
        return {}
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def job_span(
    name: str, job_id: str, meta: Mapping[str, str], **attributes: Any
) -> Iterator[None]:
    """A span for work on one job, continuing the trace whose ``traceparent`` was
    stored in the job's Redis hash at submit. Without telemetry it costs a context
    lookup and nothing is recorded."""
    if trace is None or propagate is None:
        yield
        return
    parent = propagate.extract(meta)
    attrs = {
        "taas.job_id": job_id,
        "taas.domain": meta.get("domain") or "default",
        "taas.username": meta.get("username"),
        **{f"taas.{k}": v for k, v in attributes.items()},
    }
    tracer = trace.get_tracer("taas")
    with tracer.start_as_current_span(
        name, context=parent, attributes={k: v for k, v in attrs.items() if v is not None}
    ):
        yield
