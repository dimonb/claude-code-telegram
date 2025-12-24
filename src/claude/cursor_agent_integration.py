"""Cursor Agent subprocess management.

Features:
- Async subprocess execution for cursor-agent CLI
- Stream-JSON output parsing
- Thinking/tool_call message support
- Session management via --resume
"""

import asyncio
import json
import shutil
import signal
import uuid
import os
from asyncio.subprocess import Process
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..config.settings import Settings
from ..utils.serialization import safe_serialize
from .exceptions import (
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from .integration import ClaudeResponse, StreamUpdate

logger = structlog.get_logger()
tracer = trace.get_tracer("cursor.agent")

# Tool call types used by cursor-agent
TOOL_CALL_TYPES = [
    "grepToolCall",
    "readToolCall",
    "editToolCall",
    "semSearchToolCall",
    "listToolCall",
    "shellToolCall",
    "writeToolCall",
    "globToolCall",
    "readLintsToolCall",
    "updateTodosToolCall",
    "deleteToolCall",
    "moveToolCall",
    "copyToolCall",
    "mkdirToolCall",
    "webSearchToolCall",
    "fetchToolCall",
    "searchToolCall",
    "mcpToolCall",  # MCP (Model Context Protocol) tool calls
]


def find_cursor_agent(cursor_agent_path: Optional[str] = None) -> Optional[str]:
    """Find cursor-agent CLI in common locations."""
    # First check if a specific path was provided via config
    if cursor_agent_path:
        if Path(cursor_agent_path).exists():
            return cursor_agent_path

    # Check CURSOR_AGENT_PATH environment variable

    env_path = os.environ.get("CURSOR_AGENT_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    # Check if cursor-agent is in PATH
    cursor_path = shutil.which("cursor-agent")
    if cursor_path:
        return cursor_path

    return None


class CursorAgentManager:
    """Manage cursor-agent subprocess execution."""

    def __init__(self, config: Settings):
        """Initialize cursor-agent manager with configuration."""
        self.config = config
        self.active_processes: Dict[str, Process] = {}
        # Track processes by user_id for cancellation
        self.user_processes: Dict[int, List[str]] = {}
        # Track cancellation flags per user
        self.cancelled_users: Dict[int, bool] = {}

        # Memory optimization settings
        self.max_message_buffer = 1000
        self.streaming_buffer_size = 65536  # 64KB

        # Tool tracking: map call_id -> (tool_name, span)
        # Used to associate tool_call started/completed events and create spans
        self.tool_tracking: Dict[str, tuple[str, Any]] = {}

        # Find cursor-agent binary
        self.cursor_agent_path = find_cursor_agent(
            getattr(config, "cursor_agent_binary_path", None)
        )

        if not self.cursor_agent_path:
            logger.warning(
                "cursor-agent not found in PATH. "
                "Install it or set CURSOR_AGENT_PATH environment variable."
            )

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        user_id: Optional[int] = None,
    ) -> ClaudeResponse:
        """Execute cursor-agent command."""
        if not self.cursor_agent_path:
            raise ClaudeProcessError(
                "❌ **cursor-agent not found**\n\n"
                "cursor-agent CLI is not installed or not in PATH.\n\n"
                "**Installation:**\n"
                "```\nnpm install -g @anthropic-ai/cursor-agent\n```\n\n"
                "Or set `CURSOR_AGENT_PATH` environment variable."
            )

        # Build command
        cmd = self._build_command(
            prompt, working_directory, session_id, continue_session
        )

        # Create process ID for tracking
        process_id = str(uuid.uuid4())

        # Clear tool tracking for new execution
        self.tool_tracking.clear()

        logger.info(
            "Starting cursor-agent process",
            process_id=process_id,
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        with tracer.start_as_current_span("cursor_agent.execute") as span:
            span.set_attribute("process_id", process_id)
            span.set_attribute("cwd", str(working_directory))
            span.set_attribute("session_id", session_id or "")
            span.set_attribute("continue_session", continue_session)
            span.set_attribute("cmd_args", " ".join(cmd[:10]))

            try:
                # Start process
                process = await self._start_process(cmd, working_directory)
                self.active_processes[process_id] = process

                # Track process by user_id if provided
                if user_id is not None:
                    if user_id not in self.user_processes:
                        self.user_processes[user_id] = []
                    self.user_processes[user_id].append(process_id)
                    # Reset cancellation flag for this user
                    self.cancelled_users[user_id] = False

                # Handle output with timeout
                result = await asyncio.wait_for(
                    self._handle_process_output(process, stream_callback, user_id),
                    timeout=self.config.claude_timeout_seconds,
                )

                logger.info(
                    "cursor-agent process completed successfully",
                    process_id=process_id,
                    duration_ms=result.duration_ms,
                )

                return result

            except asyncio.TimeoutError as e:
                # Kill process on timeout
                if process_id in self.active_processes:
                    self.active_processes[process_id].kill()
                    await self.active_processes[process_id].wait()

                span.record_exception(e)
                span.set_status(
                    Status(StatusCode.ERROR, description="cursor_agent_timeout")
                )

                logger.error(
                    "cursor-agent process timed out",
                    process_id=process_id,
                    timeout_seconds=self.config.claude_timeout_seconds,
                )

                raise ClaudeTimeoutError(
                    f"cursor-agent timed out after {self.config.claude_timeout_seconds}s"
                ) from e

            except Exception as e:
                span.record_exception(e)
                span.set_status(
                    Status(
                        StatusCode.ERROR,
                        description=f"cursor_agent_error: {type(e).__name__}",
                    )
                )
                logger.exception("cursor-agent process failed", process_id=process_id)
                raise

            finally:
                # Clean up
                if process_id in self.active_processes:
                    del self.active_processes[process_id]

                # Remove from user tracking
                if user_id is not None and user_id in self.user_processes:
                    if process_id in self.user_processes[user_id]:
                        self.user_processes[user_id].remove(process_id)
                    if not self.user_processes[user_id]:
                        del self.user_processes[user_id]

                # Clean up cancellation flag if process completed normally
                if user_id is not None and user_id in self.cancelled_users:
                    # Only remove if it wasn't cancelled (to avoid race condition)
                    if not self.cancelled_users.get(user_id, False):
                        del self.cancelled_users[user_id]

    def _build_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str],
        continue_session: bool,
    ) -> List[str]:
        """Build cursor-agent command with arguments."""
        cmd = [self.cursor_agent_path or "cursor-agent"]

        # Force mode - allow all commands
        if getattr(self.config, "cursor_agent_force_mode", True):
            cmd.append("-f")

        # Auto-approve MCP servers
        if getattr(self.config, "cursor_agent_approve_mcps", True):
            cmd.append("--approve-mcps")

        # Print mode for headless operation
        cmd.append("--print")

        # JSON streaming output
        cmd.extend(["--output-format", "stream-json"])
        cmd.append("--stream-partial-output")

        # Workspace directory
        cmd.extend(["--workspace", str(working_directory)])

        # Model selection
        cursor_model = getattr(self.config, "cursor_agent_model", None)
        if cursor_model:
            cmd.extend(["--model", cursor_model])

        # Resume session if continuing
        if continue_session and session_id:
            cmd.extend(["--resume", session_id])

        # Add the prompt
        cmd.append(prompt)

        logger.debug("Built cursor-agent command", command=cmd[:10])
        return cmd

    async def _start_process(self, cmd: List[str], cwd: Path) -> Process:
        """Start cursor-agent subprocess."""
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,  # Don't use stdin to avoid blocking
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            limit=1024 * 1024 * 512,  # 512MB
        )

    async def _handle_process_output(
        self,
        process: Process,
        stream_callback: Optional[Callable],
        user_id: Optional[int] = None,
    ) -> ClaudeResponse:
        """Handle cursor-agent output with streaming support."""
        with tracer.start_as_current_span("cursor_agent.output") as span:
            message_buffer: deque = deque(maxlen=self.max_message_buffer)
            result = None
            parsing_errors = []
            message_count = 0
            tool_count = 0
            thinking_content = []
            assistant_content_parts = []

            # Create cancellation check function
            def check_cancelled() -> bool:
                return user_id is not None and self.cancelled_users.get(user_id, False)

            async for line in self._read_stream_bounded(
                process.stdout, check_cancelled
            ):
                # Check if this user's process was cancelled
                if check_cancelled():
                    logger.info(
                        "Process cancelled during stream reading",
                        user_id=user_id,
                    )
                    # Try graceful cancellation
                    await self._graceful_cancel_process(process, user_id)
                    raise asyncio.CancelledError("Process cancelled by user")

                if not line:
                    continue

                try:
                    msg = json.loads(line)

                    if not self._validate_message_structure(msg):
                        parsing_errors.append(f"Invalid message: {line[:100]}")
                        continue

                    message_buffer.append(msg)
                    message_count += 1

                    msg_type = msg.get("type")

                    # Track tool calls count
                    if msg_type == "tool_call" and msg.get("subtype") == "started":
                        tool_count += 1

                    # Aggregate thinking content
                    if msg_type == "thinking":
                        text = msg.get("text", "")
                        if text:
                            thinking_content.append(text)

                    # Aggregate assistant content
                    if msg_type == "assistant":
                        content = self._extract_assistant_content(msg)
                        if content:
                            assistant_content_parts.append(content)

                    # Parse and send stream update
                    update = self._parse_stream_message(msg)
                    if update and stream_callback:
                        try:
                            await stream_callback(update)
                        except Exception as e:
                            logger.exception(
                                "Stream callback failed",
                                error=str(e),
                                update_type=update.type,
                            )

                    # Check for final result
                    if msg_type == "result":
                        result = msg

                except json.JSONDecodeError as e:
                    parsing_errors.append(f"JSON decode error: {e}")
                    logger.exception(
                        "Failed to parse JSON line", line=line[:200], error=str(e)
                    )
                    continue

            span.set_attribute("message_count", message_count)
            span.set_attribute("tool_count", tool_count)
            span.set_attribute("parsing_errors", len(parsing_errors))

            if parsing_errors:
                logger.exception(
                    "Parsing errors encountered",
                    count=len(parsing_errors),
                    errors=parsing_errors[:5],
                )

            # Wait for process to complete
            return_code = await process.wait()

            # Handle errors
            if result and result.get("is_error"):
                error_msg = result.get("result", "")
                logger.error(
                    "cursor-agent returned error",
                    return_code=return_code,
                    error_msg=error_msg,
                )
                span.set_status(
                    Status(StatusCode.ERROR, description="cursor_agent_error")
                )
                raise ClaudeProcessError(f"cursor-agent error: {error_msg}")

            if return_code != 0:
                stderr = await process.stderr.read()
                error_msg = stderr.decode("utf-8", errors="replace")
                logger.error(
                    "cursor-agent process failed",
                    return_code=return_code,
                    stderr=error_msg,
                )
                span.set_status(
                    Status(StatusCode.ERROR, description=f"exit_code_{return_code}")
                )
                raise ClaudeProcessError(
                    f"cursor-agent exited with code {return_code}: {error_msg}"
                )

            if not result:
                logger.error("No result message received from cursor-agent")
                raise ClaudeParsingError("No result message received from cursor-agent")

            # Clean up any uncompleted tool spans
            if self.tool_tracking:
                logger.warning(
                    "Cleaning up uncompleted tool spans",
                    count=len(self.tool_tracking),
                    call_ids=list(self.tool_tracking.keys()),
                )
                for call_id, (tool_name, span) in self.tool_tracking.items():
                    span.set_status(
                        Status(StatusCode.ERROR, description="Tool span not completed")
                    )
                    span.end()
                    logger.debug(
                        "Forcefully ended uncompleted tool span",
                        call_id=call_id,
                        tool_name=tool_name,
                    )
                self.tool_tracking.clear()

            return self._parse_result(
                result, list(message_buffer), assistant_content_parts
            )

    async def _read_stream_bounded(
        self, stream: Any, check_cancelled: Optional[Callable[[], bool]] = None
    ) -> AsyncIterator[str]:
        """Read stream with memory bounds and optional cancellation check."""
        buffer = b""

        while True:
            # Use wait_for with short timeout to allow cancellation checks
            try:
                chunk = await asyncio.wait_for(
                    stream.read(self.streaming_buffer_size), timeout=0.5
                )
            except asyncio.TimeoutError:
                # Check cancellation if provided
                if check_cancelled and check_cancelled():
                    break
                continue

            if not chunk:
                break

            buffer += chunk

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace").strip()

            # Check cancellation after each line
            if check_cancelled and check_cancelled():
                break

        if buffer:
            yield buffer.decode("utf-8", errors="replace").strip()

    def _validate_message_structure(self, msg: Dict) -> bool:
        """Validate message has required structure."""
        return "type" in msg

    def _extract_tool_name(self, msg: Dict) -> str:
        """Extract tool name from tool_call message."""
        tool_call_data = msg.get("tool_call", {})
        for tool_type in TOOL_CALL_TYPES:
            if tool_type in tool_call_data:
                return tool_type.replace("ToolCall", "").lower()
        return "unknown"

    def _extract_assistant_content(self, msg: Dict) -> Optional[str]:
        """Extract text content from assistant message."""
        message = msg.get("message", {})
        content_blocks = message.get("content", [])

        text_parts = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)

        return "".join(text_parts) if text_parts else None

    def _parse_stream_message(self, msg: Dict) -> Optional[StreamUpdate]:
        """Parse cursor-agent stream message into StreamUpdate."""
        msg_type = msg.get("type")

        if msg_type == "system":
            return self._parse_system_message(msg)
        elif msg_type == "user":
            return self._parse_user_message(msg)
        elif msg_type == "thinking":
            return self._parse_thinking_message(msg)
        elif msg_type == "assistant":
            return self._parse_assistant_message(msg)
        elif msg_type == "tool_call":
            return self._parse_tool_call_message(msg)
        elif msg_type == "result":
            return None  # Handled separately

        logger.debug("Unknown cursor-agent message type", msg_type=msg_type)
        return None

    def _parse_system_message(self, msg: Dict) -> StreamUpdate:
        """Parse system init message."""
        subtype = msg.get("subtype")

        return StreamUpdate(
            type="system",
            metadata={
                "subtype": subtype,
                "api_key_source": msg.get("apiKeySource"),
                "model": msg.get("model"),
                "cwd": msg.get("cwd"),
                "permission_mode": msg.get("permissionMode"),
            },
            session_context={"session_id": msg.get("session_id")},
        )

    def _parse_user_message(self, msg: Dict) -> StreamUpdate:
        """Parse user message."""
        message = msg.get("message", {})
        content_blocks = message.get("content", [])

        text_parts = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        return StreamUpdate(
            type="user",
            content="\n".join(text_parts) if text_parts else None,
            session_context={"session_id": msg.get("session_id")},
        )

    def _parse_thinking_message(self, msg: Dict) -> StreamUpdate:
        """Parse thinking message (delta or completed)."""
        subtype = msg.get("subtype")  # delta or completed

        return StreamUpdate(
            type="thinking",
            content=msg.get("text", ""),
            metadata={"subtype": subtype},
            timestamp=str(msg.get("timestamp_ms", "")),
            session_context={"session_id": msg.get("session_id")},
        )

    def _parse_assistant_message(self, msg: Dict) -> StreamUpdate:
        """Parse assistant message with content blocks."""
        message = msg.get("message", {})
        content_blocks = message.get("content", [])

        text_parts = []
        tool_calls = []

        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                            "id": block.get("id"),
                        }
                    )

        return StreamUpdate(
            type="assistant",
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls if tool_calls else None,
            timestamp=str(msg.get("timestamp_ms", "")),
            session_context={"session_id": msg.get("session_id")},
            execution_id=msg.get("model_call_id"),
        )

    def _parse_tool_call_message(self, msg: Dict) -> StreamUpdate:
        """Parse tool_call message (started or completed).

        IMPORTANT: This method creates and manages OpenTelemetry spans for tool calls:
        - On "started": Creates a span and stores it in self.tool_tracking[call_id]
        - On "completed": Retrieves the span from tracking, adds results, and closes it
        - Spans stay open between "started" and "completed" events
        - Supports parallel tool calls via unique call_id
        """
        subtype = msg.get("subtype")  # started or completed
        call_id = msg.get("call_id")
        tool_call_data = msg.get("tool_call", {})

        # Find the tool type and extract info
        tool_name = None
        tool_args = {}
        tool_result = None
        mcp_provider = None
        mcp_tool_name = None
        error_message = None
        is_error = False

        # Try to extract tool name from tool_call_data
        for tool_type in TOOL_CALL_TYPES:
            if tool_type in tool_call_data:
                tool_info = tool_call_data[tool_type]

                # Handle MCP tool calls specially
                if tool_type == "mcpToolCall":
                    # Extract MCP-specific information
                    mcp_args = tool_info.get("args", {})
                    mcp_provider = mcp_args.get("providerIdentifier", "unknown")
                    mcp_tool_name = mcp_args.get("toolName", "unknown")
                    tool_name = f"mcp_{mcp_provider}_{mcp_tool_name}"
                    tool_args = mcp_args.get("args", {})

                    if subtype == "completed":
                        tool_result = tool_info.get("result")
                else:
                    # Regular tool calls
                    tool_name = tool_type.replace("ToolCall", "").lower()
                    tool_args = tool_info.get("args", {})
                    if subtype == "completed":
                        tool_result = tool_info.get("result")
                break

        # Handle started/completed events differently
        if subtype == "started":
            # For started events, create a span and cache the tool name
            if tool_name and call_id:
                # Create OpenTelemetry span for this tool call (NOT using 'with' so it stays open)
                span = tracer.start_span(f"cursor_agent.tool.{tool_name}")
                span.set_attribute("tool.name", tool_name)
                span.set_attribute("tool.call_id", call_id)

                logger.debug(
                    "Created tool span (started)",
                    tool_name=tool_name,
                    call_id=call_id,
                    span_id=(
                        format(span.get_span_context().span_id, "016x")
                        if span.get_span_context().is_valid
                        else "invalid"
                    ),
                )

                # Add MCP-specific attributes if this is an MCP tool
                if mcp_provider and mcp_tool_name:
                    span.set_attribute("tool.mcp.provider", mcp_provider)
                    span.set_attribute("tool.mcp.tool_name", mcp_tool_name)
                    span.set_attribute("tool.type", "mcp")
                else:
                    span.set_attribute("tool.type", "cursor_agent_builtin")

                # Add session and model context
                if msg.get("model_call_id"):
                    span.set_attribute("tool.model_call_id", msg.get("model_call_id"))
                if msg.get("session_id"):
                    span.set_attribute("tool.session_id", msg.get("session_id"))
                if msg.get("timestamp_ms"):
                    span.set_attribute("tool.timestamp_ms", msg.get("timestamp_ms"))

                # Save raw message data for debugging
                try:
                    raw_message = safe_serialize(msg)
                    span.set_attribute("tool.raw_message", raw_message)
                except Exception as e:
                    logger.warning(
                        "Failed to serialize raw message",
                        error=str(e),
                        call_id=call_id,
                    )

                # Add validation attributes (cursor-agent has its own validation)
                # Check if force mode is enabled
                force_mode = getattr(self.config, "cursor_agent_force_mode", True)
                approve_mcps = getattr(self.config, "cursor_agent_approve_mcps", True)

                if force_mode:
                    span.set_attribute("tool.validated", True)
                    span.set_attribute("tool.validation_mode", "cursor_agent_force")
                    span.set_attribute("tool.approved", True)
                else:
                    # Interactive mode - validation happens in cursor-agent UI
                    span.set_attribute("tool.validated", True)
                    span.set_attribute(
                        "tool.validation_mode", "cursor_agent_interactive"
                    )

                span.set_attribute("tool.mcps_approved", approve_mcps)

                # Add all tool arguments as attributes
                if tool_args:
                    # Serialize arguments as JSON for complex types
                    try:
                        import json as json_module

                        for key, value in tool_args.items():
                            # Handle different types
                            if isinstance(value, (str, int, float, bool, type(None))):
                                # Simple types - add directly (limit string size)
                                if isinstance(value, str):
                                    span.set_attribute(
                                        f"tool.input.{key}", value[:1000]
                                    )
                                else:
                                    span.set_attribute(f"tool.input.{key}", value)
                            else:
                                # Complex types - serialize to JSON
                                try:
                                    json_value = json_module.dumps(
                                        value, ensure_ascii=False
                                    )
                                    # Limit JSON size to avoid span bloat
                                    if len(json_value) > 2000:
                                        json_value = (
                                            json_value[:2000] + "...(truncated)"
                                        )
                                    span.set_attribute(f"tool.input.{key}", json_value)
                                except (TypeError, ValueError):
                                    # If can't serialize, just use str()
                                    span.set_attribute(
                                        f"tool.input.{key}", str(value)[:1000]
                                    )
                    except Exception as e:
                        logger.warning(
                            "Failed to add tool arguments to span",
                            error=str(e),
                            call_id=call_id,
                        )

                # Store tool name and span for later
                self.tool_tracking[call_id] = (tool_name, span)

                logger.debug(
                    "Started tool span",
                    call_id=call_id,
                    tool_name=tool_name,
                    args_count=len(tool_args) if tool_args else 0,
                )
        elif subtype == "completed":
            # For completed events, try to get tool name from tracking first
            if call_id and call_id in self.tool_tracking:
                cached_tool_name, span = self.tool_tracking[call_id]
                if not tool_name:
                    # Use cached name if we couldn't extract from data
                    tool_name = cached_tool_name

                # Save raw completion message for debugging
                try:
                    raw_completion = safe_serialize(msg)
                    span.set_attribute("tool.raw_completion", raw_completion)
                except Exception as e:
                    logger.warning(
                        "Failed to serialize raw completion message",
                        error=str(e),
                        call_id=call_id,
                    )

                # Add result and output to span
                if tool_result:
                    import json as json_module

                    # Add result type
                    span.set_attribute("tool.result.type", type(tool_result).__name__)

                    try:
                        # Handle MCP result structure specially
                        if isinstance(tool_result, dict) and "success" in tool_result:
                            # MCP result format
                            success_data = tool_result.get("success", {})
                            is_error = success_data.get("isError", False)

                            # Extract content from MCP response
                            content_blocks = success_data.get("content", [])
                            text_parts = []
                            for block in content_blocks:
                                if isinstance(block, dict) and "text" in block:
                                    text_block = block.get("text", {})
                                    if isinstance(text_block, dict):
                                        text_parts.append(text_block.get("text", ""))
                                    else:
                                        text_parts.append(str(text_block))

                            if text_parts:
                                mcp_output = "\n".join(text_parts)
                                span.set_attribute("tool.result.size", len(mcp_output))
                                # Limit MCP output size
                                if len(mcp_output) > 5000:
                                    mcp_output = mcp_output[:5000] + "\n...(truncated)"
                                span.set_attribute("tool.output", mcp_output)

                            span.set_attribute("tool.mcp.is_error", is_error)
                            span.set_attribute(
                                "tool.status", "error" if is_error else "success"
                            )
                            if is_error and not error_message:
                                error_message = success_data.get("message") or (
                                    mcp_output if text_parts else None
                                )

                            # Serialize full MCP result
                            try:
                                json_result = json_module.dumps(
                                    tool_result, ensure_ascii=False
                                )
                                if len(json_result) > 5000:
                                    json_result = json_result[:5000] + "...(truncated)"
                                span.set_attribute("tool.result", json_result)
                            except (TypeError, ValueError):
                                span.set_attribute(
                                    "tool.result", str(tool_result)[:5000]
                                )

                        # Handle different result types
                        elif isinstance(tool_result, str):
                            # String result - add directly
                            span.set_attribute("tool.result.size", len(tool_result))
                            # Add full result (limit size to avoid span bloat)
                            result_preview = tool_result[:5000]
                            if len(tool_result) > 5000:
                                result_preview += "\n...(truncated)"
                            span.set_attribute("tool.output", result_preview)

                            # Check if result contains error indicators
                            if any(
                                err in tool_result.lower()
                                for err in ["error:", "failed:", "exception:"]
                            ):
                                is_error = True
                                if not error_message:
                                    error_message = tool_result
                        elif isinstance(tool_result, (int, float, bool, type(None))):
                            # Simple types
                            span.set_attribute("tool.output", str(tool_result))
                        elif isinstance(tool_result, dict):
                            # Dict result - extract output if present
                            if "output" in tool_result:
                                output_str = str(tool_result["output"])
                                span.set_attribute("tool.output", output_str[:5000])
                            if "error" in tool_result:
                                error_msg = str(tool_result["error"])
                                span.set_attribute("tool.error", error_msg)
                                span.set_attribute("tool.validation_error", error_msg)
                                is_error = True
                                error_message = error_message or error_msg
                            if "status" in tool_result:
                                status = tool_result["status"]
                                span.set_attribute("tool.status", str(status))
                                if status in ["error", "failed", "rejected"]:
                                    is_error = True
                                    if not error_message:
                                        error_message = str(
                                            tool_result.get("error") or status
                                        )

                            # Serialize full result as JSON
                            try:
                                json_result = json_module.dumps(
                                    tool_result, ensure_ascii=False
                                )
                                if len(json_result) > 5000:
                                    json_result = json_result[:5000] + "...(truncated)"
                                span.set_attribute("tool.result", json_result)
                            except (TypeError, ValueError):
                                span.set_attribute(
                                    "tool.result", str(tool_result)[:5000]
                                )
                        else:
                            # Other types - serialize to JSON or str
                            try:
                                json_result = json_module.dumps(
                                    tool_result, ensure_ascii=False
                                )
                                if len(json_result) > 5000:
                                    json_result = json_result[:5000] + "...(truncated)"
                                span.set_attribute("tool.output", json_result)
                            except (TypeError, ValueError):
                                span.set_attribute(
                                    "tool.output", str(tool_result)[:5000]
                                )
                    except Exception as e:
                        logger.warning(
                            "Failed to add tool result to span",
                            error=str(e),
                            call_id=call_id,
                        )
                        # Fallback to simple string representation
                        span.set_attribute("tool.output", str(tool_result)[:1000])

                # Set span status based on whether there was an error
                if is_error:
                    span.set_status(
                        Status(StatusCode.ERROR, description="tool_execution_failed")
                    )
                else:
                    span.set_status(Status(StatusCode.OK))

                # Close the span
                span.end()

                logger.debug(
                    "Closed tool span (completed)",
                    call_id=call_id,
                    tool_name=tool_name,
                    has_result=bool(tool_result),
                    is_error=is_error,
                    span_id=(
                        format(span.get_span_context().span_id, "016x")
                        if span.get_span_context().is_valid
                        else "invalid"
                    ),
                )

                # Clean up tracking entry
                del self.tool_tracking[call_id]

        # Log warning if tool name is still unknown
        if not tool_name:
            logger.warning(
                "Unable to determine tool name",
                call_id=call_id,
                subtype=subtype,
                tool_call_data_keys=list(tool_call_data.keys()),
                msg_snippet=str(msg)[:200],
            )

        # Use tool_result or tool_call type for update type
        update_type = "tool_result" if subtype == "completed" else "tool_call"

        metadata = {
            "subtype": subtype,
            "call_id": call_id,
            "tool_use_id": call_id,  # Alias for compatibility with message.py
            "tool_name": tool_name or "unknown",
        }
        if tool_args:
            metadata["tool_args"] = tool_args
        if subtype == "completed":
            metadata["status"] = "error" if is_error else "success"
            metadata["is_error"] = is_error
        else:
            metadata["status"] = "running"

        return StreamUpdate(
            type=update_type,
            metadata=metadata,
            tool_calls=(
                [
                    {
                        "name": tool_name,
                        "input": tool_args,
                        "id": call_id,
                        "result": tool_result,
                    }
                ]
                if tool_name
                else None
            ),
            timestamp=str(msg.get("timestamp_ms", "")),
            session_context={"session_id": msg.get("session_id")},
            error_info={"message": error_message} if error_message else None,
        )

    def _parse_result(
        self,
        result: Dict,
        messages: List[Dict],
        assistant_content_parts: List[str],
    ) -> ClaudeResponse:
        """Parse final result message into ClaudeResponse."""
        # Extract tools used from messages
        tools_used = []
        for msg in messages:
            if msg.get("type") == "tool_call" and msg.get("subtype") == "started":
                tool_name = self._extract_tool_name(msg)
                tools_used.append(
                    {
                        "name": tool_name,
                        "timestamp": msg.get("timestamp_ms"),
                    }
                )

        # Get content from result or aggregated assistant messages (no truncation)
        content = result.get("result", "")
        if not content and assistant_content_parts:
            content = "".join(assistant_content_parts)

        # Count assistant turns
        num_turns = len(
            [
                m
                for m in messages
                if m.get("type") == "assistant" and m.get("message", {}).get("content")
            ]
        )

        return ClaudeResponse(
            content=content,
            session_id=result.get("session_id", ""),
            cost=0.0,  # cursor-agent doesn't provide cost
            duration_ms=result.get("duration_ms", 0),
            num_turns=num_turns,
            is_error=result.get("is_error", False),
            error_type=result.get("subtype") if result.get("is_error") else None,
            tools_used=tools_used,
        )

    def _extract_final_content(self, content: str) -> str:
        """Extract final summary/result from cursor-agent output.

        Cursor-agent includes all intermediate thoughts in the result.
        This method extracts only the final meaningful content.
        """
        if not content:
            return content

        import re

        # Try to find Summary section (common pattern)
        # Look for ## Summary, ### Summary, # Summary, **Summary**, Summary:
        summary_patterns = [
            r"(?:^|\n)(#{1,3}\s*Summary\s*\n)",  # ## Summary
            r"(?:^|\n)(\*\*Summary\*\*\s*\n)",  # **Summary**
            r"(?:^|\n)(Summary:\s*\n)",  # Summary:
            r"(?:^|\n)(#{1,3}\s*Changes\s+Made:?\s*\n)",  # ## Changes Made
            r"(?:^|\n)(#{1,3}\s*Result:?\s*\n)",  # ## Result
        ]

        for pattern in summary_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                # Return everything from the match onwards
                return content[match.start() :].strip()

        # If no summary found, try to extract the last substantial block
        # Split by double newlines and find meaningful sections
        blocks = re.split(r"\n\n+", content.strip())

        # Filter out very short blocks that are likely intermediate thoughts
        # (e.g., "Checking...", "Reading...", "Looking for...")
        intermediate_patterns = [
            r"^(?:Checking|Reading|Looking|Searching|Trying|Getting|Extracting|"
            r"Retrying|Finding|Implementation|Implementing|Now|Let me|I'll|"
            r"I need to|First,|Next,|Then,)\s",
        ]

        substantial_blocks = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Skip short blocks that match intermediate patterns
            is_intermediate = False
            for pat in intermediate_patterns:
                if re.match(pat, block, re.IGNORECASE):
                    # Only skip if it's a short block (single line or very short)
                    if len(block) < 200 and block.count("\n") < 3:
                        is_intermediate = True
                        break

            if not is_intermediate:
                substantial_blocks.append(block)

        # If we have substantial blocks, return them joined
        if substantial_blocks:
            # If there are many blocks, return only the last few (likely the conclusion)
            if len(substantial_blocks) > 3:
                return "\n\n".join(substantial_blocks[-3:])
            return "\n\n".join(substantial_blocks)

        # Fallback: return original content
        return content

    async def _graceful_cancel_process(
        self, process: Process, user_id: Optional[int] = None
    ) -> None:
        """Gracefully cancel a process using multiple strategies.

        Tries in order:
        1. Send SIGINT signal (interrupt, like Ctrl+C)
        2. Send SIGTERM signal (termination)
        3. Force kill with SIGKILL (last resort)
        """
        try:
            # Strategy 1: Send SIGINT (interrupt signal, like Ctrl+C)
            if process.returncode is None:
                try:
                    logger.debug(
                        "Sending SIGINT to process",
                        user_id=user_id,
                        pid=process.pid,
                    )
                    process.send_signal(signal.SIGINT)
                    # Give process time to handle SIGINT (max 2 seconds)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                        logger.info(
                            "Process cancelled gracefully via SIGINT",
                            user_id=user_id,
                        )
                        return
                    except asyncio.TimeoutError:
                        logger.debug(
                            "Process didn't respond to SIGINT, trying SIGTERM",
                            user_id=user_id,
                        )
                except ProcessLookupError:
                    # Process already terminated
                    return
                except Exception as e:
                    logger.debug(
                        "Failed to send SIGINT, trying SIGTERM",
                        user_id=user_id,
                        error=str(e),
                    )

            # Strategy 2: Send SIGTERM (termination signal)
            if process.returncode is None:
                try:
                    logger.debug(
                        "Sending SIGTERM to process",
                        user_id=user_id,
                        pid=process.pid,
                    )
                    process.terminate()
                    # Give process time to handle SIGTERM (max 2 seconds)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                        logger.info(
                            "Process cancelled gracefully via SIGTERM",
                            user_id=user_id,
                        )
                        return
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Process didn't respond to SIGTERM, forcing kill",
                            user_id=user_id,
                        )
                except ProcessLookupError:
                    # Process already terminated
                    return
                except Exception as e:
                    logger.warning(
                        "Failed to send SIGTERM, forcing kill",
                        user_id=user_id,
                        error=str(e),
                    )

            # Strategy 3: Force kill (last resort)
            if process.returncode is None:
                try:
                    logger.warning(
                        "Force killing process (last resort)",
                        user_id=user_id,
                        pid=process.pid,
                    )
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    # Process already terminated
                    pass
                except Exception as e:
                    logger.warning(
                        "Failed to force kill process",
                        user_id=user_id,
                        error=str(e),
                    )

        except Exception as e:
            logger.warning(
                "Error during graceful cancellation",
                user_id=user_id,
                error=str(e),
            )

    async def kill_all_processes(self) -> None:
        """Terminate all active cursor-agent processes gracefully."""
        logger.info(
            "Terminating all active cursor-agent processes",
            count=len(self.active_processes),
        )

        process_ids = list(self.active_processes.keys())
        for process_id in process_ids:
            if process_id in self.active_processes:
                process = self.active_processes[process_id]
                await self._graceful_cancel_process(process)

        self.active_processes.clear()
        self.user_processes.clear()

    async def kill_user_processes(self, user_id: int) -> None:
        """Terminate all active processes for a specific user gracefully."""
        # Set cancellation flag first to stop stream reading
        self.cancelled_users[user_id] = True

        if user_id not in self.user_processes:
            return

        process_ids = self.user_processes[user_id].copy()
        logger.info(
            "Terminating cursor-agent processes for user (graceful shutdown)",
            user_id=user_id,
            count=len(process_ids),
        )

        for process_id in process_ids:
            if process_id in self.active_processes:
                process = self.active_processes[process_id]
                await self._graceful_cancel_process(process, user_id=user_id)
                # Clean up
                if process_id in self.active_processes:
                    del self.active_processes[process_id]

        # Clean up user tracking
        if user_id in self.user_processes:
            del self.user_processes[user_id]

        # Clean up cancellation flag
        if user_id in self.cancelled_users:
            del self.cancelled_users[user_id]

    def get_active_process_count(self) -> int:
        """Get number of active processes."""
        return len(self.active_processes)
