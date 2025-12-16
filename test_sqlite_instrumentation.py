"""Test script to verify SQLite3 instrumentation works with aiosqlite."""

import asyncio
import tempfile
from pathlib import Path
from typing import List

import aiosqlite
from opentelemetry import trace
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SpanExporter,
    SpanExportResult,
    BatchSpanProcessor,
)
from opentelemetry.sdk.trace import ReadableSpan


class TestSpanExporter(SpanExporter):
    """Span exporter that collects spans in memory."""

    def __init__(self):
        self.spans: List[ReadableSpan] = []

    def export(self, spans: List[ReadableSpan]) -> SpanExportResult:
        """Export spans to memory."""
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Shutdown exporter."""
        pass


async def test_aiosqlite_instrumentation():
    """Test if SQLite3Instrumentor captures aiosqlite operations."""
    # Setup OpenTelemetry
    tracer_provider = TracerProvider()
    test_exporter = TestSpanExporter()
    span_processor = BatchSpanProcessor(test_exporter)
    tracer_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(tracer_provider)

    # Instrument SQLite
    instrumentor = SQLite3Instrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)

    # Create temporary database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        print("Testing aiosqlite with SQLite3Instrumentor...")

        # Test 1: Direct connection
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, name TEXT)"
            )
            await db.execute("INSERT INTO test (name) VALUES (?)", ("test_value",))
            await db.commit()

            async with db.execute("SELECT * FROM test") as cursor:
                async for row in cursor:
                    print(f"Row: {row}")

        # Force flush spans
        span_processor.force_flush()

        # Check results
        print(f"\n📊 Captured {len(test_exporter.spans)} spans:")
        sqlite_spans = [
            s
            for s in test_exporter.spans
            if "sqlite" in s.name.lower() or "db" in s.name.lower()
        ]

        if sqlite_spans:
            print(f"✅ Found {len(sqlite_spans)} SQLite-related spans!")
            for span in sqlite_spans[:5]:
                attrs = {k: v for k, v in span.attributes.items() if k}
                print(f"  - {span.name}: {attrs}")
        else:
            print(
                "❌ No SQLite spans found - instrumentation may not be working with aiosqlite"
            )
            if test_exporter.spans:
                print(f"   All span names: {[s.name for s in test_exporter.spans]}")
            else:
                print("   No spans captured at all!")

    finally:
        # Cleanup
        Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(test_aiosqlite_instrumentation())
