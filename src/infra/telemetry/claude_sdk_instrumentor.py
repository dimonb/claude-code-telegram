"""OpenTelemetry instrumentation for claude-agent-sdk.

This instrumentor wraps the claude_agent_sdk.query function to automatically
capture spans for all SDK calls, including tool usage, streaming messages,
and error conditions.
"""

import contextlib
import functools
import sys
from typing import Any, AsyncIterator

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = structlog.get_logger()


class ClaudeSDKInstrumentor:
    """OpenTelemetry instrumentor for claude-agent-sdk."""

    _instance = None
    _instrumented = False
    _original_query = None
    _instrumented_query = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def instrument(self, tracer_provider=None) -> None:
        """Instrument claude-agent-sdk.query function.

        Args:
            tracer_provider: Optional tracer provider (for compatibility with other instrumentors).
        """
        if self._instrumented:
            return

        try:
            import claude_agent_sdk

            # Store reference to original function BEFORE any patching
            original_query = claude_agent_sdk.query
            self._original_query = original_query

            from claude_agent_sdk.types import (
                AssistantMessage,
                ResultMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
            )

            @functools.wraps(original_query)
            async def instrumented_query(
                prompt: str, options: Any = None, **kwargs: Any
            ) -> AsyncIterator[Any]:
                """Instrumented version of claude_agent_sdk.query."""
                # Create span manually (not as context manager) to avoid
                # context issues when generator is interrupted (GeneratorExit)
                tracer = trace.get_tracer(__name__)
                sdk_span = tracer.start_span("claude_agent_sdk.query")

                sdk_span.set_attribute("claude_agent_sdk.prompt_length", len(prompt))
                # Add prompt text (truncated if too long)
                prompt_text = prompt[:1000] + (
                    "... (truncated)" if len(prompt) > 1000 else ""
                )
                sdk_span.set_attribute("claude_agent_sdk.prompt", prompt_text)

                if options:
                    cwd = getattr(options, "cwd", None)
                    if cwd:
                        sdk_span.set_attribute("claude_agent_sdk.cwd", str(cwd))
                    max_turns = getattr(options, "max_turns", None)
                    if max_turns:
                        sdk_span.set_attribute("claude_agent_sdk.max_turns", max_turns)
                    allowed_tools = getattr(options, "allowed_tools", None)
                    if allowed_tools:
                        sdk_span.set_attribute(
                            "claude_agent_sdk.allowed_tools",
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
                # Track active tool spans to add results later
                active_tool_spans = {}

                try:
                    # Use sdk_span as the parent context for all child spans
                    with trace.use_span(sdk_span):
                        # claude_agent_sdk.query requires keyword-only arguments
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
                                            tool_id = getattr(block, "id", None)
                                            tool_name = getattr(
                                                block, "name", "unknown"
                                            )
                                            logger.debug(
                                                "ToolUseBlock received",
                                                tool_name=tool_name,
                                                tool_id=tool_id,
                                            )
                                            # Create a child span for each tool call
                                            # Keep it open until we get the result
                                            tool_span = tracer.start_span(
                                                f"tool.{tool_name}"
                                            )
                                            tool_span.set_attribute(
                                                "tool_name", tool_name
                                            )
                                            tool_input = getattr(block, "input", {})
                                            tool_span.set_attribute(
                                                "tool_input",
                                                str(tool_input)[:500],
                                            )
                                            if tool_id:
                                                tool_span.set_attribute(
                                                    "tool_id", tool_id
                                                )
                                                # Store span to add result later
                                                active_tool_spans[tool_id] = tool_span
                                                logger.debug(
                                                    "Tool span created and stored",
                                                    tool_name=tool_name,
                                                    tool_id=tool_id,
                                                )
                                            else:
                                                # No ID, can't match result - end immediately
                                                logger.warning(
                                                    "ToolUseBlock without ID",
                                                    tool_name=tool_name,
                                                )
                                                tool_span.end()
                                        elif isinstance(block, ToolResultBlock):
                                            # Match result to tool span
                                            tool_use_id = getattr(
                                                block, "tool_use_id", None
                                            )
                                            logger.debug(
                                                "ToolResultBlock received",
                                                tool_use_id=tool_use_id,
                                                has_span=tool_use_id in active_tool_spans if tool_use_id else False,
                                                active_spans_count=len(active_tool_spans),
                                            )
                                            if (
                                                tool_use_id
                                                and tool_use_id in active_tool_spans
                                            ):
                                                tool_span = active_tool_spans.pop(
                                                    tool_use_id
                                                )
                                                # Add result to span
                                                content = getattr(block, "content", "")
                                                is_error = getattr(
                                                    block, "is_error", False
                                                )
                                                if content:
                                                    result_preview = str(content)[:100]
                                                    tool_span.set_attribute(
                                                        "tool_result",
                                                        str(content)[:1000],
                                                    )
                                                    logger.debug(
                                                        "Tool result captured",
                                                        tool_use_id=tool_use_id,
                                                        result_length=len(str(content)),
                                                        result_preview=result_preview,
                                                    )
                                                tool_span.set_attribute(
                                                    "tool_is_error", bool(is_error)
                                                )
                                                if is_error:
                                                    tool_span.set_status(
                                                        Status(
                                                            StatusCode.ERROR,
                                                            description="Tool execution failed",
                                                        )
                                                    )
                                                # End the tool span now that we have the result
                                                tool_span.end()
                                            else:
                                                logger.warning(
                                                    "ToolResultBlock without matching span",
                                                    tool_use_id=tool_use_id,
                                                    active_spans=list(active_tool_spans.keys()),
                                                )

                            elif isinstance(message, ResultMessage):
                                cost = getattr(message, "total_cost_usd", None)
                                if cost is not None:
                                    total_cost = float(cost) or 0.0
                                    sdk_span.set_attribute(
                                        "claude_agent_sdk.cost_usd", total_cost
                                    )

                    # Normal completion - set final attributes
                    sdk_span.set_attribute(
                        "claude_agent_sdk.message_count", message_count
                    )
                    sdk_span.set_attribute(
                        "claude_agent_sdk.assistant_messages_count",
                        assistant_messages_count,
                    )
                    sdk_span.set_attribute("claude_agent_sdk.tool_count", tool_count)
                    sdk_span.set_attribute(
                        "claude_agent_sdk.text_blocks_count", text_blocks_count
                    )
                    if response_text_parts:
                        response_text = "".join(response_text_parts)
                        response_text_attr = (
                            response_text[:1000] + "... (truncated)"
                            if len(response_text) > 1000
                            else response_text
                        )
                        sdk_span.set_attribute(
                            "claude_agent_sdk.response_text", response_text_attr
                        )

                except GeneratorExit:
                    # Generator closed by consumer - mark as interrupted, not error
                    sdk_span.set_attribute(
                        "claude_agent_sdk.generator_interrupted", True
                    )
                    sdk_span.set_attribute(
                        "claude_agent_sdk.message_count", message_count
                    )
                    # End any active tool spans before re-raising
                    for tool_id, tool_span in active_tool_spans.items():
                        with contextlib.suppress(ValueError, RuntimeError):
                            tool_span.set_attribute("tool_result_missing", True)
                            tool_span.set_attribute("interrupted", True)
                            tool_span.end()
                    active_tool_spans.clear()
                    raise

                except Exception as e:
                    sdk_span.record_exception(e)
                    error_str = str(e)
                    error_str_lower = error_str.lower()

                    # Check if this is a JSON decode error (will trigger fallback)
                    is_json_error = (
                        "json" in error_str_lower
                        and (
                            "decode" in error_str_lower or "parsing" in error_str_lower
                        )
                    ) or "unterminated string" in error_str_lower

                    if (
                        "limit reached" in error_str_lower
                        or "usage limit" in error_str_lower
                    ):
                        sdk_span.set_attribute(
                            "claude_agent_sdk.error_type", "usage_limit_reached"
                        )
                        sdk_span.set_status(
                            Status(StatusCode.ERROR, description=str(e))
                        )
                    elif is_json_error:
                        # JSON decode errors trigger fallback, not real errors
                        sdk_span.set_attribute(
                            "claude_agent_sdk.error_type", "json_decode_will_fallback"
                        )
                        sdk_span.set_attribute(
                            "claude_agent_sdk.fallback_triggered", True
                        )
                        # Don't mark as ERROR - this will be retried via subprocess
                        sdk_span.set_status(
                            Status(
                                StatusCode.OK,
                                description="SDK failed, will retry via subprocess",
                            )
                        )
                    else:
                        sdk_span.set_status(
                            Status(StatusCode.ERROR, description=str(e))
                        )

                    # End any active tool spans
                    for tool_id, tool_span in active_tool_spans.items():
                        with contextlib.suppress(ValueError, RuntimeError):
                            if is_json_error:
                                # JSON error - mark for fallback, not as error
                                tool_span.set_attribute("sdk_incomplete", True)
                                tool_span.set_attribute(
                                    "sdk_incomplete_reason", "json_decode_error"
                                )
                                tool_span.set_attribute(
                                    "note",
                                    "SDK failed with JSON error, operation retried via subprocess",
                                )
                                # Don't mark as error since fallback will complete it
                                tool_span.set_status(
                                    Status(
                                        StatusCode.OK,
                                        description="Retried via subprocess fallback",
                                    )
                                )
                            else:
                                # Real error
                                tool_span.set_attribute("tool_result_missing", True)
                                tool_span.set_status(
                                    Status(StatusCode.ERROR, description="SDK error")
                                )
                            tool_span.end()
                    active_tool_spans.clear()
                    raise

                finally:
                    # End any remaining tool spans that never got results
                    for tool_id, tool_span in active_tool_spans.items():
                        with contextlib.suppress(ValueError, RuntimeError):
                            tool_span.set_attribute("tool_result_missing", True)
                            tool_span.end()

                    # Always end the span, suppressing context detach errors
                    # that can occur when generator is interrupted across tasks
                    with contextlib.suppress(ValueError, RuntimeError):
                        sdk_span.end()

            # Patch the module - this will affect future imports
            claude_agent_sdk.query = instrumented_query

            # Also patch in sys.modules to catch already-imported references
            if "claude_agent_sdk" in sys.modules:
                sys.modules["claude_agent_sdk"].query = instrumented_query

            # Store reference to instrumented query for delayed patching
            self._instrumented_query = instrumented_query

            # CRITICAL: Patch already-imported modules that imported query directly
            # This handles the case where sdk_integration.py does:
            # `from claude_agent_sdk import query`
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
        """Uninstrument claude-agent-sdk."""
        if not self._instrumented:
            return

        try:
            # Note: We don't restore original since we don't store it
            # In practice, uninstrument is rarely used
            self._instrumented = False
        except ImportError:
            pass
