"""OpenTelemetry instrumentation for aiosqlite.

This module provides automatic instrumentation for aiosqlite to capture
SQL query details (statements, parameters, row counts) in OpenTelemetry spans.
"""

import functools
import json
from typing import Any, Callable, Optional, Tuple, Union

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


class AiosqliteInstrumentor:
    """Instrumentor for aiosqlite that captures SQL query details."""

    _instance = None
    _instrumented = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def instrument(self, tracer_provider=None):
        """Instrument aiosqlite connection methods."""
        if self._instrumented:
            return

        try:
            import aiosqlite

            # Patch Connection.execute
            original_execute = aiosqlite.Connection.execute

            @functools.wraps(original_execute)
            async def instrumented_execute(self, sql: str, parameters=None):
                """Instrumented execute method."""
                span = trace.get_current_span()
                if span and span.is_recording():
                    # Add SQL statement
                    span.set_attribute("db.statement", sql.strip() if sql else "")

                    # Add parameters if provided
                    if parameters:
                        AiosqliteInstrumentor._add_parameters_to_span(span, parameters)

                try:
                    # Execute original method
                    cursor = await original_execute(self, sql, parameters)

                    # Add row count for SELECT queries (will be updated after fetch)
                    if span and span.is_recording():
                        # Store reference to cursor for later row count update
                        if hasattr(cursor, "rowcount"):
                            span.set_attribute("db.rows_affected", cursor.rowcount)

                    return cursor
                except Exception as e:
                    if span and span.is_recording():
                        span.record_exception(e)
                        span.set_status(Status(StatusCode.ERROR, description=str(e)))
                    raise

            # Patch Connection.executemany
            original_executemany = aiosqlite.Connection.executemany

            @functools.wraps(original_executemany)
            async def instrumented_executemany(self, sql: str, parameters_seq):
                """Instrumented executemany method."""
                span = trace.get_current_span()
                if span and span.is_recording():
                    span.set_attribute("db.statement", sql.strip() if sql else "")
                    span.set_attribute(
                        "db.statement.parameters.count", len(parameters_seq)
                    )
                    # Show sample of first parameter set
                    if parameters_seq:
                        AiosqliteInstrumentor._add_parameters_to_span(
                            span,
                            parameters_seq[0],
                            prefix="db.statement.parameters.sample",
                        )

                try:
                    cursor = await original_executemany(self, sql, parameters_seq)
                    if span and span.is_recording() and hasattr(cursor, "rowcount"):
                        span.set_attribute("db.rows_affected", cursor.rowcount)
                    return cursor
                except Exception as e:
                    if span and span.is_recording():
                        span.record_exception(e)
                        span.set_status(Status(StatusCode.ERROR, description=str(e)))
                    raise

            # Patch Cursor.fetchone and fetchall to add row count
            # Note: In aiosqlite, these are async methods that return coroutines
            # We need to intercept them before they're called
            original_fetchone = aiosqlite.Cursor.fetchone

            @functools.wraps(original_fetchone)
            async def instrumented_fetchone(self):
                """Instrumented fetchone method."""
                row = await original_fetchone(self)
                span = trace.get_current_span()
                if span and span.is_recording():
                    span.set_attribute("db.row_count", 1 if row is not None else 0)
                return row

            original_fetchall = aiosqlite.Cursor.fetchall

            @functools.wraps(original_fetchall)
            async def instrumented_fetchall(self):
                """Instrumented fetchall method."""
                rows = await original_fetchall(self)
                span = trace.get_current_span()
                if span and span.is_recording():
                    span.set_attribute("db.row_count", len(rows))
                return rows

            # Apply patches
            aiosqlite.Connection.execute = instrumented_execute
            aiosqlite.Connection.executemany = instrumented_executemany
            aiosqlite.Cursor.fetchone = instrumented_fetchone
            aiosqlite.Cursor.fetchall = instrumented_fetchall

            self._instrumented = True

        except ImportError:
            # aiosqlite not installed, skip instrumentation
            pass

    @staticmethod
    def _add_parameters_to_span(
        span: trace.Span,
        parameters: Union[Tuple, list, dict],
        prefix: str = "db.statement.parameters",
    ):
        """Add SQL parameters to span in safe format."""
        try:
            if isinstance(parameters, (tuple, list)):
                # Positional parameters
                params_list = list(parameters)
                span.set_attribute(f"{prefix}.count", len(params_list))
                if len(params_list) > 0:
                    # Show first 3 parameters as sample (truncated for safety)
                    sample_params = params_list[:3]
                    safe_params = []
                    for p in sample_params:
                        if isinstance(p, str) and len(p) > 100:
                            safe_params.append(f"{p[:100]}...")
                        elif isinstance(p, (bytes, bytearray)):
                            safe_params.append(f"<bytes:{len(p)}>")
                        else:
                            safe_params.append(str(p))
                    span.set_attribute(f"{prefix}.sample", json.dumps(safe_params))
            elif isinstance(parameters, dict):
                # Named parameters
                param_keys = list(parameters.keys())
                span.set_attribute(f"{prefix}.keys", json.dumps(param_keys))
                # Show sample values (first 3, truncated)
                sample_params = {}
                for k, v in list(parameters.items())[:3]:
                    if isinstance(v, str) and len(v) > 100:
                        sample_params[k] = f"{v[:100]}..."
                    elif isinstance(v, (bytes, bytearray)):
                        sample_params[k] = f"<bytes:{len(v)}>"
                    else:
                        sample_params[k] = str(v)
                if sample_params:
                    span.set_attribute(f"{prefix}.sample", json.dumps(sample_params))
        except Exception:
            # If serialization fails, skip parameters silently
            pass

    def uninstrument(self):
        """Uninstrument aiosqlite."""
        if not self._instrumented:
            return

        try:
            import aiosqlite

            # Restore original methods if we saved them
            # Note: This is a simplified version; in production you'd want
            # to properly restore original methods
            self._instrumented = False
        except ImportError:
            pass
