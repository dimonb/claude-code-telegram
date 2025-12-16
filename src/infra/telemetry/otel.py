"""OpenTelemetry/Uptrace configuration utilities.

This module configures:
* Structured logging via structlog
* Optional OTLP log export to Uptrace
* Tracing provider and OTLP span export
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Dict
from urllib.parse import urlparse

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry._logs import set_logger_provider

from src import __version__
from src.config.settings import Settings
from src.infra.telemetry.aiosqlite_instrumentor import AiosqliteInstrumentor
from src.infra.telemetry.claude_sdk_instrumentor import ClaudeSDKInstrumentor
from src.utils.serialization import safe_serialize


def _build_resource(settings: Settings) -> Resource:
    """Build OTEL resource describing this service."""
    attributes: Dict[str, Any] = {
        "service.name": settings.telemetry_service_name,
        "service.version": __version__,
        "deployment.environment": (
            "production" if settings.is_production else "development"
        ),
    }

    return Resource.create(attributes)


def configure_logging(settings: Settings) -> None:
    """Configure structured logging and optional OTLP log export.

    This function replaces the ad-hoc logging configuration from ``src.main``
    and centralizes all logging/telemetry behaviour.
    """
    # Base Python logging configuration
    level_name = settings.telemetry_log_level or settings.log_level
    level = getattr(logging, level_name.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    # Configure structlog
    # When telemetry is enabled, prefer JSON logs regardless of DEBUG flag,
    # so that Uptrace/OTEL always receives structured events.
    if settings.telemetry_enabled:
        use_json = settings.telemetry_json_log
    else:
        use_json = not settings.debug

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.EventRenamer("event"),
            (
                structlog.processors.JSONRenderer()
                if use_json
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure OTLP log export only when telemetry is enabled
    if not settings.telemetry_enabled:
        return

    resource = _build_resource(settings)

    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)

    log_exporter = OTLPLogExporter()
    log_processor = BatchLogRecordProcessor(log_exporter)
    logger_provider.add_log_record_processor(log_processor)

    # Bridge standard logging into OTEL
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)


def configure_tracing(settings: Settings) -> None:
    """Configure OTEL tracing and Uptrace exporter."""
    if not settings.telemetry_enabled:
        return

    resource = _build_resource(settings)

    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter()
    span_processor = BatchSpanProcessor(span_exporter)
    tracer_provider.add_span_processor(span_processor)

    trace.set_tracer_provider(tracer_provider)

    # Instrument HTTP clients (httpx is used by python-telegram-bot and anthropic SDK)
    HTTPXClientInstrumentor().instrument(
        tracer_provider=tracer_provider,
        request_hook=httpx_request_hook,
        response_hook=httpx_response_hook,
        async_request_hook=httpx_async_request_hook,
        async_response_hook=httpx_async_response_hook,
    )

    # Instrument logging to link logs with spans
    LoggingInstrumentor().instrument(
        set_logging_format=True, tracer_provider=tracer_provider
    )

    # Instrument aiosqlite to capture SQL query details (statements, parameters, row counts)
    # Note: opentelemetry-instrumentation-sqlite3 does NOT work with aiosqlite
    # because aiosqlite executes operations in a separate thread.
    # We use our custom instrumentor instead.
    AiosqliteInstrumentor().instrument(tracer_provider=tracer_provider)

    # Instrument claude-code-sdk to automatically trace all SDK queries
    # This provides low-level instrumentation of the SDK's query function
    ClaudeSDKInstrumentor().instrument(tracer_provider=tracer_provider)


def _mask_telegram_bot_token(text: str) -> str:
    """Mask Telegram bot token in path/URL.

    Converts /bot123456:AAH59IhRTHC1c24TxjRc7UE7AZHG12WlcOg/action
    to /bot123456:AA**Og/action
    """
    # Pattern: /bot{digits}:{token}/
    # Token is alphanumeric, typically 35+ characters
    pattern = r"(/bot\d+:)([A-Za-z0-9_-]{4,})(/?)"

    def replace_token(match: re.Match) -> str:
        prefix = match.group(1)  # /bot123456:
        token = match.group(2)  # The actual token
        suffix = match.group(3)  # Optional trailing /

        if len(token) > 4:
            # Keep first 2 and last 2 characters, mask the rest
            masked_token = f"{token[:2]}**{token[-2:]}"
        else:
            # If token is too short, just mask it all
            masked_token = "****"

        return f"{prefix}{masked_token}{suffix}"

    return re.sub(pattern, replace_token, text)


def _extract_and_mask_url(request: Any) -> tuple[str | None, str | None]:
    """Extract path and full URL from request, with Telegram token masking.

    Returns:
        tuple: (masked_path, masked_url) or (None, None) if extraction fails
    """
    if not hasattr(request, "url"):
        return None, None

    url = request.url
    path = None
    full_url_str = None

    try:
        # Try to get path from URL object
        if hasattr(url, "path"):
            path = url.path
            # Get full URL string
            full_url_str = str(url)
        elif hasattr(url, "raw_path"):
            raw_path = url.raw_path
            if isinstance(raw_path, bytes):
                raw_path = raw_path.decode("utf-8")
            path = raw_path.split("?")[0] if "?" in raw_path else raw_path
            full_url_str = str(url)
        elif isinstance(url, str):
            parsed = urlparse(url)
            path = parsed.path
            full_url_str = url
    except Exception:
        return None, None

    # Mask Telegram bot tokens
    if path:
        path = _mask_telegram_bot_token(path)
    if full_url_str:
        full_url_str = _mask_telegram_bot_token(full_url_str)

    return path, full_url_str


def httpx_request_hook(span: Any, request: Any) -> None:
    """Hook for httpx synchronous request."""
    if span and span.is_recording():
        try:
            # Extract and mask http.route and http.url
            path, full_url = _extract_and_mask_url(request)
            if path:
                span.set_attribute("http.route", path)
            if full_url:
                span.set_attribute("http.url", full_url)

            if hasattr(request, "headers"):
                span.set_attribute(
                    "http.request.headers", json.dumps(dict(request.headers))
                )

            # Extract request body from stream (like in tillabuybot)
            body_content = None
            if hasattr(request, "stream") and hasattr(request.stream, "_stream"):
                try:
                    # HTTPX stores body in stream._stream
                    body_content = request.stream._stream
                except Exception:
                    pass
            elif hasattr(request, "content"):
                body_content = request.content

            if body_content is not None:
                span.set_attribute("http.request.body", safe_serialize(body_content))
        except Exception:
            pass


def httpx_response_hook(span: Any, request: Any, response: Any) -> None:
    """Hook for httpx synchronous response."""
    if span and span.is_recording():
        try:
            if hasattr(response, "headers"):
                span.set_attribute(
                    "http.response.headers", json.dumps(dict(response.headers))
                )
            if hasattr(response, "content"):
                # Limit response body size
                content = response.content
                if isinstance(content, bytes) and len(content) > 1000:
                    content = content[:1000] + b"... (truncated)"
                span.set_attribute("http.response.body", safe_serialize(content))
        except Exception:
            pass


async def httpx_async_request_hook(span: Any, request: Any) -> None:
    """Hook for httpx async request."""
    if span and span.is_recording():
        try:
            # Extract and mask http.route and http.url
            path, full_url = _extract_and_mask_url(request)
            if path:
                span.set_attribute("http.route", path)
            if full_url:
                span.set_attribute("http.url", full_url)

            if hasattr(request, "headers"):
                span.set_attribute(
                    "http.request.headers", json.dumps(dict(request.headers))
                )

            # Extract request body from stream (like in tillabuybot)
            body_content = None
            if hasattr(request, "stream") and hasattr(request.stream, "_stream"):
                try:
                    # HTTPX stores body in stream._stream
                    body_content = request.stream._stream
                except Exception:
                    pass
            elif hasattr(request, "content"):
                body_content = request.content

            if body_content is not None:
                span.set_attribute("http.request.body", safe_serialize(body_content))
        except Exception:
            pass


async def httpx_async_response_hook(span: Any, request: Any, response: Any) -> None:
    """Hook for httpx async response."""
    if span and span.is_recording():
        try:
            if hasattr(response, "headers"):
                span.set_attribute(
                    "http.response.headers", json.dumps(dict(response.headers))
                )
            # Only capture response body if it's already available (not streaming)
            # Don't intercept streams as it can cause connection pool issues
            if hasattr(response, "content"):
                try:
                    # For non-streaming responses, content is available immediately
                    content = response.content
                    if isinstance(content, bytes) and len(content) > 1000:
                        content = content[:1000] + b"... (truncated)"
                    elif isinstance(content, str) and len(content) > 1000:
                        content = content[:1000] + "... (truncated)"
                    span.set_attribute("http.response.body", safe_serialize(content))
                except Exception:
                    # If content is not available yet (streaming), skip it
                    pass
        except Exception:
            pass
