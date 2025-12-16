"""OpenTelemetry instrumentation for claude-code-sdk.

This instrumentor wraps the claude_code_sdk.query function to automatically
capture spans for all SDK calls, including tool usage, streaming messages,
and error conditions.
"""

import contextlib
import functools
import sys
from typing import Any, AsyncIterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


class ClaudeSDKInstrumentor:
    """OpenTelemetry instrumentor for claude-code-sdk."""

    _instance = None
    _instrumented = False
    _original_query = None
    _instrumented_query = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def instrument(self, tracer_provider=None) -> None:
        """Instrument claude-code-sdk.query function.

        Args:
            tracer_provider: Optional tracer provider (for compatibility with other instrumentors).
        """
        if self._instrumented:
            return

        try:
            import claude_code_sdk

            # Store reference to original function BEFORE any patching
            original_query = claude_code_sdk.query
            self._original_query = original_query

            from claude_code_sdk.types import (
                AssistantMessage,
                ResultMessage,
                TextBlock,
                ToolUseBlock,
            )

            @functools.wraps(original_query)
            async def instrumented_query(
                prompt: str, options: Any = None, **kwargs: Any
            ) -> AsyncIterator[Any]:
                """Instrumented version of claude_code_sdk.query."""
                # Create span manually (not as context manager) to avoid
                # context issues when generator is interrupted (GeneratorExit)
                tracer = trace.get_tracer(__name__)
                sdk_span = tracer.start_span("claude_code_sdk.query")

                sdk_span.set_attribute("claude_code_sdk.prompt_length", len(prompt))
                # Add prompt text (truncated if too long)
                prompt_text = prompt[:1000] + (
                    "... (truncated)" if len(prompt) > 1000 else ""
                )
                sdk_span.set_attribute("claude_code_sdk.prompt", prompt_text)

                if options:
                    cwd = getattr(options, "cwd", None)
                    if cwd:
                        sdk_span.set_attribute("claude_code_sdk.cwd", str(cwd))
                    max_turns = getattr(options, "max_turns", None)
                    if max_turns:
                        sdk_span.set_attribute("claude_code_sdk.max_turns", max_turns)
                    allowed_tools = getattr(options, "allowed_tools", None)
                    if allowed_tools:
                        sdk_span.set_attribute(
                            "claude_code_sdk.allowed_tools",
                            (
                                ",".join(allowed_tools)
                                if isinstance(allowed_tools, list)
                                else str(allowed_tools)
                            ),
                        )

                message_count = 0
                tool_count = 0
                assistant_messages_count = 0
                text_blocks_count = 0
                total_cost = 0.0
                response_text_parts = []

                try:
                    # Use sdk_span as the parent context for all child spans
                    with trace.use_span(sdk_span):
                        # claude_code_sdk.query requires keyword-only arguments
                        async for message in original_query(
                            prompt=prompt, options=options, **kwargs
                        ):
                            message_count += 1
                            yield message

                            # Track message types and extract data
                            if isinstance(message, AssistantMessage):
                                assistant_messages_count += 1
                                content = getattr(message, "content", [])
                                if content and isinstance(content, list):
                                    for block in content:
                                        if isinstance(block, TextBlock):
                                            text_blocks_count += 1
                                            text = getattr(block, "text", "")
                                            if text:
                                                response_text_parts.append(text)
                                        elif isinstance(block, ToolUseBlock):
                                            tool_count += 1
                                            tool_name = getattr(
                                                block, "tool_name", "unknown"
                                            )
                                            # Create a child span for each tool call
                                            # This makes tool calls visible in traces
                                            with tracer.start_as_current_span(
                                                f"tool.{tool_name}"
                                            ) as tool_span:
                                                tool_span.set_attribute("tool.name", tool_name)
                                                tool_input = getattr(block, "tool_input", {})
                                                tool_span.set_attribute(
                                                    "tool.input",
                                                    str(tool_input)[:500],
                                                )
                                                # The span will be automatically ended when exiting the context

                        elif isinstance(message, ResultMessage):
                            cost = getattr(message, "total_cost_usd", None)
                            if cost is not None:
                                total_cost = float(cost) or 0.0
                                sdk_span.set_attribute(
                                    "claude_code_sdk.cost_usd", total_cost
                                )

                    # Normal completion - set final attributes
                    sdk_span.set_attribute(
                        "claude_code_sdk.message_count", message_count
                    )
                    sdk_span.set_attribute(
                        "claude_code_sdk.assistant_messages_count",
                        assistant_messages_count,
                    )
                    sdk_span.set_attribute("claude_code_sdk.tool_count", tool_count)
                    sdk_span.set_attribute(
                        "claude_code_sdk.text_blocks_count", text_blocks_count
                    )
                    if response_text_parts:
                        response_text = "".join(response_text_parts)
                        response_text_attr = (
                            response_text[:1000] + "... (truncated)"
                            if len(response_text) > 1000
                            else response_text
                        )
                        sdk_span.set_attribute(
                            "claude_code_sdk.response_text", response_text_attr
                        )

                except GeneratorExit:
                    # Generator closed by consumer - mark as interrupted, not error
                    sdk_span.set_attribute(
                        "claude_code_sdk.generator_interrupted", True
                    )
                    sdk_span.set_attribute(
                        "claude_code_sdk.message_count", message_count
                    )
                    raise

                except Exception as e:
                    sdk_span.record_exception(e)
                    sdk_span.set_status(Status(StatusCode.ERROR, description=str(e)))
                    error_str = str(e).lower()
                    if "limit reached" in error_str or "usage limit" in error_str:
                        sdk_span.set_attribute(
                            "claude_code_sdk.error_type", "usage_limit_reached"
                        )
                    raise

                finally:
                    # Always end the span, suppressing context detach errors
                    # that can occur when generator is interrupted across tasks
                    with contextlib.suppress(ValueError, RuntimeError):
                        sdk_span.end()

            # Patch the module - this will affect future imports
            claude_code_sdk.query = instrumented_query

            # Also patch in sys.modules to catch already-imported references
            if "claude_code_sdk" in sys.modules:
                sys.modules["claude_code_sdk"].query = instrumented_query

            # Store reference to instrumented query for delayed patching
            self._instrumented_query = instrumented_query

            # CRITICAL: Patch already-imported modules that imported query directly
            # This handles the case where sdk_integration.py does:
            # `from claude_code_sdk import query`
            # which creates a local reference before our patch
            self._patch_imported_modules(original_query, instrumented_query)

            self._instrumented = True

        except ImportError:
            # SDK not installed, skip instrumentation
            pass

    def _patch_imported_modules(self, original_query, instrumented_query):
        """Patch all modules that already imported query directly."""
        for module_name, module in list(sys.modules.items()):
            if not module:
                continue
            try:
                # Check if this module has 'query' attribute that matches original
                if hasattr(module, "query"):
                    module_query = getattr(module, "query", None)
                    # Compare function objects - if they're the same, patch it
                    if callable(module_query) and module_query is original_query:
                        setattr(module, "query", instrumented_query)
                        # Debug: log which modules we patched
                        import structlog

                        logger = structlog.get_logger()
                        logger.debug(
                            "Patched query in module",
                            module_name=module_name,
                        )
            except (AttributeError, TypeError, ValueError):
                # Skip if we can't access/inspect the module
                pass

    def _ensure_patched(self):
        """Ensure all modules are patched (call when modules might be imported later)."""
        if (
            not self._instrumented
            or not self._original_query
            or not self._instrumented_query
        ):
            return

        # Re-patch modules that might have been imported after initial instrumentation
        self._patch_imported_modules(self._original_query, self._instrumented_query)

    def uninstrument(self) -> None:
        """Uninstrument claude-code-sdk."""
        if not self._instrumented:
            return

        try:
            # Note: We don't restore original since we don't store it
            # In practice, uninstrument is rarely used
            self._instrumented = False
        except ImportError:
            pass
