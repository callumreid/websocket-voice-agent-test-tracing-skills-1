"""Coval OpenTelemetry export helpers for the WebSocket test agent."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any
from typing import Iterable
from typing import Sequence

import requests
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

logger = logging.getLogger(__name__)

COVAL_TRACES_ENDPOINT = os.environ.get("COVAL_TRACES_ENDPOINT", "https://api.coval.dev/v1/traces")
COVAL_API_KEY = os.environ.get("COVAL_API_KEY", "")
SESSION_ID_ATTR = "session.id"
_MAX_BUFFERED_SPANS_PER_SESSION = int(os.environ.get("COVAL_MAX_BUFFERED_SPANS_PER_SESSION", "500"))

_exporter: "CovalSessionExporter | None" = None


def setup_coval_tracing(service_name: str) -> None:
    """Install a process-wide tracer provider with a session-routed Coval exporter."""
    global _exporter
    if _exporter is not None:
        return
    if not COVAL_API_KEY:
        logger.warning("COVAL_API_KEY not set; Coval trace export is disabled")
        return

    _exporter = CovalSessionExporter(api_key=COVAL_API_KEY, endpoint=COVAL_TRACES_ENDPOINT)
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(provider)
    logger.info("Coval tracing initialized for service=%s", service_name)


def get_tracer():
    return trace.get_tracer("coval.websocket-test-agent")


def set_simulation_id(session_id: str, simulation_id: str) -> None:
    if _exporter is None:
        return
    _exporter.set_simulation_id(session_id, simulation_id)


def clear_session(session_id: str) -> None:
    if _exporter is None:
        return
    _exporter.clear_session(session_id)


class CovalSessionExporter(SpanExporter):
    """Export spans to Coval using the simulation ID associated with each session.

    WebSocket simulation IDs arrive in the initialization JSON after the socket
    is accepted. Spans that end before that message arrives are buffered by
    `session.id` and flushed once the ID is known.
    """

    def __init__(self, *, api_key: str, endpoint: str, timeout: int = 30) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout
        self._simulation_ids_by_session: dict[str, str] = {}
        self._buffers_by_session: dict[str, list[ReadableSpan]] = defaultdict(list)

    def set_simulation_id(self, session_id: str, simulation_id: str) -> None:
        self._simulation_ids_by_session[session_id] = simulation_id
        buffered = self._buffers_by_session.pop(session_id, [])
        if buffered:
            logger.info("Flushing %d buffered span(s) for session=%s", len(buffered), session_id)
            self._export_for_simulation(buffered, simulation_id)

    def clear_session(self, session_id: str) -> None:
        self._simulation_ids_by_session.pop(session_id, None)
        dropped = len(self._buffers_by_session.pop(session_id, []))
        if dropped:
            logger.warning("Dropped %d buffered span(s) without a simulation ID for session=%s", dropped, session_id)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        spans_by_session: dict[str, list[ReadableSpan]] = defaultdict(list)
        for span in spans:
            session_id = _span_session_id(span)
            if not session_id:
                logger.debug("Dropping span without session.id: %s", span.name)
                continue
            spans_by_session[session_id].append(span)

        for session_id, session_spans in spans_by_session.items():
            simulation_id = self._simulation_ids_by_session.get(session_id)
            if simulation_id:
                if not self._export_for_simulation(session_spans, simulation_id):
                    return SpanExportResult.FAILURE
                continue

            buffer = self._buffers_by_session[session_id]
            buffer.extend(session_spans)
            overflow = len(buffer) - _MAX_BUFFERED_SPANS_PER_SESSION
            if overflow > 0:
                del buffer[:overflow]
                logger.warning(
                    "Coval trace buffer exceeded %d spans for session=%s; dropped oldest %d span(s)",
                    _MAX_BUFFERED_SPANS_PER_SESSION,
                    session_id,
                    overflow,
                )
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        self._simulation_ids_by_session.clear()
        self._buffers_by_session.clear()

    def _export_for_simulation(self, spans: Sequence[ReadableSpan], simulation_id: str) -> bool:
        payload = _spans_to_otlp_json(spans)
        try:
            response = requests.post(
                self._endpoint,
                json=payload,
                headers={"x-api-key": self._api_key, "X-Simulation-Id": simulation_id},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Coval trace export failed for simulation_id=%s: %s", simulation_id, exc)
            return False

        if response.status_code == 200:
            logger.info("Coval trace export accepted spans=%d simulation_id=%s", len(spans), simulation_id)
            return True

        logger.warning(
            "Coval trace export rejected spans=%d simulation_id=%s status=%s body=%s",
            len(spans),
            simulation_id,
            response.status_code,
            response.text[:500],
        )
        return False


def _span_session_id(span: ReadableSpan) -> str:
    value = span.attributes.get(SESSION_ID_ATTR) if span.attributes else None
    return value if isinstance(value, str) and value else ""


def _spans_to_otlp_json(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    if not spans:
        return {"resourceSpans": []}

    resource = spans[0].resource if spans[0].resource else None
    scope_name = spans[0].instrumentation_scope.name if spans[0].instrumentation_scope else "coval.websocket-test-agent"
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes(resource.attributes if resource else {})},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name},
                        "spans": [_span_to_otlp_json(span) for span in spans],
                    }
                ],
            }
        ]
    }


def _span_to_otlp_json(span: ReadableSpan) -> dict[str, Any]:
    context = span.context
    return {
        "traceId": _hex_id(context.trace_id, 16) if context else "",
        "spanId": _hex_id(context.span_id, 8) if context else "",
        "parentSpanId": _hex_id(span.parent.span_id, 8) if span.parent else "",
        "name": span.name,
        "kind": span.kind.value,
        "startTimeUnixNano": str(span.start_time or 0),
        "endTimeUnixNano": str(span.end_time or 0),
        "attributes": _attributes(span.attributes or {}),
        "status": {
            "code": span.status.status_code.value,
            "message": span.status.description or "",
        },
        "events": [
            {
                "timeUnixNano": str(event.timestamp or 0),
                "name": event.name,
                "attributes": _attributes(event.attributes or {}),
            }
            for event in span.events
        ],
        "links": [],
    }


def _attributes(attributes: Iterable[tuple[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    items = attributes.items() if hasattr(attributes, "items") else attributes
    return [{"key": str(key), "value": _attribute_value(value)} for key, value in items]


def _attribute_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_attribute_value(item) for item in value[:20]]}}
    return {"stringValue": str(value)}


def _hex_id(value: int, byte_length: int) -> str:
    return format(value, f"0{byte_length * 2}x") if value else ""
