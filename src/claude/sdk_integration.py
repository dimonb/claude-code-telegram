"""Claude Code Python SDK integration.

Features:
- Native Claude Code SDK integration
- Async streaming support
- Tool execution management
- Session persistence
"""

import asyncio
import glob
import os
import uuid
import shutil
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import structlog
from opentelemetry import trace
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    query as claude_query,
    ClaudeSDKError,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    CLIJSONDecodeError,
)
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..config.settings import Settings
from ..security.validators import SecurityValidator
from .exceptions import (
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from .hooks import SecurityHooks

logger = structlog.get_logger()
tracer = trace.get_tracer("claude.sdk")

# Type alias for SDK message types
Message = Union[AssistantMessage, UserMessage, ResultMessage]


def find_claude_cli(claude_cli_path: Optional[str] = None) -> Optional[str]:
    """Find Claude CLI in common locations."""

    # First check if a specific path was provided via config or env
    if claude_cli_path:
        if os.path.exists(claude_cli_path) and os.access(claude_cli_path, os.X_OK):
            return claude_cli_path

    # Check CLAUDE_CLI_PATH environment variable
    env_path = os.environ.get("CLAUDE_CLI_PATH")
    if env_path and os.path.exists(env_path) and os.access(env_path, os.X_OK):
        return env_path

    # Check if claude is already in PATH
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    # Check common installation locations
    common_paths = [
        # NVM installations
        os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
        # Direct npm global install
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/node_modules/.bin/claude"),
        # System locations
        "/usr/local/bin/claude",
        "/usr/bin/claude",
        # Windows locations (for cross-platform support)
        os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
    ]

    for pattern in common_paths:
        matches = glob.glob(pattern)
        if matches:
            # Return the first match
            return matches[0]

    return None


def update_path_for_claude(claude_cli_path: Optional[str] = None) -> bool:
    """Update PATH to include Claude CLI if found."""
    claude_path = find_claude_cli(claude_cli_path)

    if claude_path:
        # Add the directory containing claude to PATH
        claude_dir = os.path.dirname(claude_path)
        current_path = os.environ.get("PATH", "")

        if claude_dir not in current_path:
            os.environ["PATH"] = f"{claude_dir}:{current_path}"
            logger.info("Updated PATH for Claude CLI", claude_path=claude_path)

        return True

    return False


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result', 'tool_result', 'error', 'progress'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None
    progress: Optional[Dict] = None

    def is_error(self) -> bool:
        """Check if this is an error update."""
        return self.type == "error" or (
            self.metadata and self.metadata.get("is_error", False)
        )

    def get_error_message(self) -> str:
        """Get error message from update."""
        if self.metadata and "error" in self.metadata:
            return str(self.metadata["error"])
        return self.content or "Unknown error"

    def get_tool_names(self) -> List[str]:
        """Get tool names from tool_calls."""
        if not self.tool_calls:
            return []
        return [call.get("name", "unknown") for call in self.tool_calls]

    def get_progress_percentage(self) -> Optional[int]:
        """Get progress percentage if available."""
        if self.progress:
            return self.progress.get("percentage")
        return None


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(self, config: Settings):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.active_sessions: Dict[str, Dict[str, Any]] = {}

        # Try to find and update PATH for Claude CLI
        if not update_path_for_claude(config.claude_cli_path):
            logger.warning(
                "Claude CLI not found in PATH or common locations. "
                "SDK may fail if Claude is not installed or not in PATH."
            )

        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK."""
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Check if we should continue an existing session
            previous_messages = []
            if continue_session and session_id and session_id in self.active_sessions:
                session_data = self.active_sessions[session_id]
                previous_messages = session_data.get("messages", [])
                logger.info(
                    "Continuing existing session",
                    session_id=session_id,
                    previous_message_count=len(previous_messages),
                )
            else:
                logger.info(
                    "Starting new session",
                    session_id=session_id,
                    continue_session=continue_session,
                    has_active_session=session_id in self.active_sessions if session_id else False,
                )

            # Create security hooks for this request
            security_hooks = SecurityHooks(
                config=self.config,
                working_directory=working_directory,
                security_validator=SecurityValidator(
                    approved_directory=self.config.approved_directory
                ),
            )
            hooks_config = security_hooks.create_hooks_config()

            # Build Claude Agent options
            options = ClaudeAgentOptions(
                max_turns=self.config.claude_max_turns,
                cwd=str(working_directory),
                allowed_tools=self.config.claude_allowed_tools,
                permission_mode="acceptEdits",  # Auto-accept file edits
                hooks=hooks_config,  # Security validation hooks
            )

            # Collect NEW messages from this query
            # (previous messages are used for context but not duplicated in response)
            messages = []
            cost = 0.0
            tools_used = []

            # Execute with streaming and timeout
            # Pass previous messages for context
            await asyncio.wait_for(
                self._execute_query_with_streaming(
                    prompt, options, messages, stream_callback, previous_messages
                ),
                timeout=self.config.claude_timeout_seconds,
            )

            # Extract cost and tools from result message, check for errors
            cost = 0.0
            tools_used = []
            result_error = None
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    tools_used = self._extract_tools_from_messages(messages)
                    # Check for error in result
                    if getattr(message, "is_error", False):
                        result_error = getattr(message, "result", "") or str(message)
                    break

            # Handle result with error flag
            if result_error:
                error_lower = result_error.lower()
                # Check for limit reached
                if "limit reached" in error_lower:
                    import re

                    time_match = re.search(
                        r"resets?\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*[apm]{0,2})",
                        result_error,
                        re.IGNORECASE,
                    )
                    timezone_match = re.search(r"\(([^)]+)\)", result_error)
                    reset_time = time_match.group(1) if time_match else "later"
                    timezone = timezone_match.group(1) if timezone_match else ""

                    logger.warning(
                        "Claude usage limit reached",
                        reset_time=reset_time,
                        timezone=timezone,
                    )

                    user_friendly_msg = (
                        f"⏱️ **Claude AI Usage Limit Reached**\n\n"
                        f"You've reached your Claude AI usage limit for this period.\n\n"
                        f"**When will it reset?**\n"
                        f"Your limit will reset at **{reset_time}**"
                        f"{f' ({timezone})' if timezone else ''}\n\n"
                        f"**What you can do:**\n"
                        f"• Wait for the limit to reset automatically\n"
                        f"• Try again after the reset time\n"
                        f"• Use simpler requests that require less processing\n"
                        f"• Contact support if you need a higher limit"
                    )
                    raise ClaudeProcessError(user_friendly_msg)

                # Other errors from result
                raise ClaudeProcessError(f"Claude returned error: {result_error}")

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Get or create session ID
            final_session_id = session_id or str(uuid.uuid4())

            # Update session with ALL messages (previous + new)
            all_messages = previous_messages + messages if previous_messages else messages
            self._update_session(final_session_id, all_messages)
            logger.debug(
                "Session updated",
                session_id=final_session_id,
                total_messages=len(all_messages),
                new_messages=len(messages),
            )

            # Extract content for attributes
            content = self._extract_content_from_messages(messages)
            num_turns = len(
                [m for m in messages if isinstance(m, (UserMessage, AssistantMessage))]
            )

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=num_turns,
                tools_used=tools_used,
            )

        except asyncio.TimeoutError as e:
            logger.exception(
                "Claude SDK command timed out",
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            ) from e

        except CLINotFoundError as e:
            logger.exception("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg) from e

        except ProcessError as e:
            error_str = str(e)

            # Check for usage limit error
            if (
                "limit reached" in error_str.lower()
                or "usage limit" in error_str.lower()
            ):

                time_match = re.search(
                    r"resets?\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*[apm]{0,2})",
                    error_str,
                    re.IGNORECASE,
                )
                timezone_match = re.search(r"\(([^)]+)\)", error_str)

                reset_time = time_match.group(1) if time_match else "later"
                timezone = timezone_match.group(1) if timezone_match else ""

                logger.warning(
                    "Claude usage limit reached",
                    reset_time=reset_time,
                    timezone=timezone,
                )

                user_friendly_msg = (
                    f"⏱️ **Claude AI Usage Limit Reached**\n\n"
                    f"You've reached your Claude AI usage limit for this period.\n\n"
                    f"**When will it reset?**\n"
                    f"Your limit will reset at **{reset_time}**"
                    f"{f' ({timezone})' if timezone else ''}\n\n"
                    f"**What you can do:**\n"
                    f"• Wait for the limit to reset automatically\n"
                    f"• Try again after the reset time\n"
                    f"• Use simpler requests that require less processing\n"
                    f"• Contact support if you need a higher limit"
                )
                raise ClaudeProcessError(user_friendly_msg)

            logger.exception(
                "Claude process failed",
                exit_code=getattr(e, "exit_code", None),
            )
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            logger.exception("Claude connection error", error=str(e))
            raise ClaudeProcessError(f"Failed to connect to Claude: {str(e)}")

        except ClaudeSDKError as e:
            error_str = str(e)

            # Check for usage limit error
            if (
                "limit reached" in error_str.lower()
                or "usage limit" in error_str.lower()
            ):
                time_match = re.search(
                    r"resets?\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*[apm]{0,2})",
                    error_str,
                    re.IGNORECASE,
                )
                timezone_match = re.search(r"\(([^)]+)\)", error_str)

                reset_time = time_match.group(1) if time_match else "later"
                timezone = timezone_match.group(1) if timezone_match else ""

                logger.warning(
                    "Claude usage limit reached",
                    reset_time=reset_time,
                    timezone=timezone,
                )

                user_friendly_msg = (
                    f"⏱️ **Claude AI Usage Limit Reached**\n\n"
                    f"You've reached your Claude AI usage limit for this period.\n\n"
                    f"**When will it reset?**\n"
                    f"Your limit will reset at **{reset_time}**"
                    f"{f' ({timezone})' if timezone else ''}\n\n"
                    f"**What you can do:**\n"
                    f"• Wait for the limit to reset automatically\n"
                    f"• Try again after the reset time\n"
                    f"• Use simpler requests that require less processing\n"
                    f"• Contact support if you need a higher limit"
                )
                raise ClaudeProcessError(user_friendly_msg) from e

            logger.exception("Claude SDK error", error=error_str)
            raise ClaudeProcessError(f"Claude SDK error: {error_str}") from e

        except RuntimeError as e:
            # Handle cancel scope errors gracefully
            error_msg = str(e)
            if (
                "cancel scope" in error_msg.lower()
                or "different task" in error_msg.lower()
            ):
                logger.warning(
                    "Cancel scope error in Claude SDK (likely due to task cancellation)",
                    error=error_msg,
                )
                # Check if we have messages with error info
                for msg in messages:
                    if isinstance(msg, ResultMessage) and getattr(
                        msg, "is_error", False
                    ):
                        result_error = getattr(msg, "result", "") or str(msg)
                        raise ClaudeProcessError(
                            f"Claude error: {result_error}"
                        ) from None
                # If no result message, suppress the cancel scope error
                # It's likely a side effect of proper cancellation
                raise ClaudeProcessError("Claude SDK operation was cancelled") from None
            else:
                # Re-raise other RuntimeErrors
                logger.exception("RuntimeError in Claude SDK")
                raise ClaudeProcessError(f"Claude SDK runtime error: {error_msg}")

        except Exception as e:
            # Handle ExceptionGroup from TaskGroup operations (Python 3.11+)
            if type(e).__name__ == "ExceptionGroup" or hasattr(e, "exceptions"):
                logger.exception(
                    "Task group error in Claude SDK",
                    exception_count=len(getattr(e, "exceptions", [])),
                    exceptions=[
                        str(ex) for ex in getattr(e, "exceptions", [])[:3]
                    ],  # Log first 3 exceptions
                )
                # Extract the most relevant exception from the group
                exceptions = getattr(e, "exceptions", [e])
                main_exception = exceptions[0] if exceptions else e
                error_str = str(main_exception)

                # Check if JSON decode error contains session-related information
                if (
                    "Failed to decode JSON" in error_str
                    or "JSONDecodeError" in error_str
                ):
                    # JSON decode errors are often symptoms of underlying issues
                    # Try to extract more context from the error message
                    if (
                        "session" in error_str.lower()
                        or "conversation" in error_str.lower()
                    ):
                        # If session info is in the error, preserve it
                        raise ClaudeProcessError(f"Claude SDK error: {error_str}")
                    else:
                        # Generic JSON decode error - will trigger fallback
                        raise ClaudeProcessError(f"Claude SDK task error: {error_str}")
                else:
                    raise ClaudeProcessError(f"Claude SDK task error: {error_str}")

            # Check if it's an ExceptionGroup disguised as a regular exception
            elif hasattr(e, "__notes__") and "TaskGroup" in str(e):
                logger.exception(
                    "TaskGroup related error in Claude SDK",
                )
                raise ClaudeProcessError(f"Claude SDK task error: {str(e)}")

            # If already a ClaudeProcessError, just re-raise without wrapping
            elif isinstance(e, ClaudeProcessError):
                raise

            else:
                logger.exception(
                    "Unexpected error in Claude SDK",
                )
                raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _execute_query_with_streaming(
        self, prompt: str, options, messages: List, stream_callback: Optional[Callable],
        previous_messages: Optional[List] = None
    ) -> None:
        """Execute query with streaming and collect messages.

        Args:
            prompt: User's prompt
            options: ClaudeAgentOptions
            messages: List to collect response messages
            stream_callback: Optional callback for streaming updates
            previous_messages: Previous messages for session continuation
        """
        message_count = 0
        tool_count = 0
        text_blocks_count = 0
        assistant_messages_count = 0

        # Build conversation context if continuing session
        if previous_messages:
            logger.info(
                "Building conversation context for continuation",
                previous_message_count=len(previous_messages),
            )
            # Extract recent conversation summary for context
            context_parts = []
            # Get last few exchanges (user + assistant pairs)
            recent_messages = previous_messages[-6:] if len(previous_messages) > 6 else previous_messages
            for msg in recent_messages:
                if isinstance(msg, UserMessage):
                    content = getattr(msg, "content", "")
                    if content:
                        # Only include the actual text content
                        if isinstance(content, str):
                            context_parts.append(f"Previous user: {content[:200]}")
                elif isinstance(msg, AssistantMessage):
                    content = self._extract_content_from_messages([msg])
                    if content:
                        context_parts.append(f"Previous response: {content[:200]}")

            if context_parts:
                context_summary = "\n".join(context_parts)
                prompt = f"[Previous conversation context]\n{context_summary}\n\n[Current request]\n{prompt}"
                logger.debug("Added conversation context to prompt", context_length=len(context_summary))

        # Use ClaudeSDKClient for streaming
        try:
            # Create client with options
            async with ClaudeSDKClient(options=options) as client:
                # Send query
                await client.query(prompt)

                # Receive streaming responses
                async for message in client.receive_response():
                    messages.append(message)
                    message_count += 1

                    # Check for ResultMessage with error - handle immediately
                    if isinstance(message, ResultMessage):
                        if getattr(message, "is_error", False):
                            result_error = getattr(message, "result", "") or str(message)
                            logger.warning(
                                "Received result message with error",
                                error=result_error,
                                is_error=True,
                            )
                            # Store error info for later processing
                            # Don't raise here - let the stream finish and handle in execute_command
                            break  # Exit streaming loop since we got the final result

                    # Track message types
                    if isinstance(message, AssistantMessage):
                        assistant_messages_count += 1
                        content = getattr(message, "content", [])
                        if content and isinstance(content, list):
                            for block in content:
                                if isinstance(block, TextBlock):
                                    text_blocks_count += 1
                                elif isinstance(block, ToolUseBlock):
                                    tool_count += 1

                    # Handle streaming callback
                    if stream_callback:
                        try:
                            await self._handle_stream_message(message, stream_callback)
                        except Exception as callback_error:
                            logger.warning(
                                "Stream callback failed",
                                error=str(callback_error),
                                error_type=type(callback_error).__name__,
                            )

        except RuntimeError as e:
            # Handle cancel scope errors gracefully
            error_msg = str(e)
            if (
                "cancel scope" in error_msg.lower()
                or "different task" in error_msg.lower()
            ):
                logger.warning(
                    "Cancel scope error in streaming (likely due to task cancellation)",
                    error=error_msg,
                )
                # Check if we have a ResultMessage with error before re-raising
                for msg in messages:
                    if isinstance(msg, ResultMessage) and getattr(
                        msg, "is_error", False
                    ):
                        result_error = getattr(msg, "result", "") or str(msg)
                        logger.warning(
                            "Found result message with error during cancel scope handling",
                            error=result_error,
                        )
                        raise ClaudeProcessError(
                            f"Claude error: {result_error}"
                        ) from None
                # If no result message, raise a cancellation error
                # This will be caught and handled properly by execute_command
                raise ClaudeProcessError("Claude SDK operation was cancelled") from None
            else:
                # Re-raise other RuntimeErrors
                raise

        except Exception as e:
            # Check if we already have a ResultMessage with error before re-raising
            for msg in messages:
                if isinstance(msg, ResultMessage) and getattr(msg, "is_error", False):
                    result_error = getattr(msg, "result", "") or str(msg)
                    logger.warning(
                        "Found result message with error during exception handling",
                        error=result_error,
                        original_exception=str(e),
                    )
                    # Re-raise with the actual error from result, not the SDK error
                    raise ClaudeProcessError(f"Claude error: {result_error}") from e

            if type(e).__name__ == "ExceptionGroup" or hasattr(e, "exceptions"):
                logger.exception("TaskGroup error in streaming execution")
            else:
                logger.exception("Error in streaming execution")
            raise
        # Note: ClaudeSDKClient context manager handles cleanup automatically

    async def _handle_stream_message(
        self, message: Message, stream_callback: Callable[[StreamUpdate], None]
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    # Extract text from TextBlock objects
                    text_parts = []
                    tool_calls = []

                    for block in content:
                        # Handle TextBlock
                        if isinstance(block, TextBlock):
                            text = getattr(block, "text", "")
                            if text:
                                text_parts.append(text)
                        # Handle ToolUseBlock
                        elif isinstance(block, ToolUseBlock):
                            tool_name = getattr(block, "name", "unknown")
                            tool_id = getattr(block, "id", None)
                            tool_input = getattr(block, "input", {})
                            tool_calls.append({
                                "name": tool_name,
                                "id": tool_id,
                                "input": tool_input
                            })

                    # Send text content if available
                    if text_parts:
                        update = StreamUpdate(
                            type="assistant",
                            content="\n".join(text_parts),
                        )
                        await stream_callback(update)

                    # Send tool calls if available
                    if tool_calls:
                        update = StreamUpdate(
                            type="assistant",
                            tool_calls=tool_calls,
                        )
                        await stream_callback(update)

                elif content:
                    # Fallback for other content types (e.g., single TextBlock)
                    if isinstance(content, str):
                        content_str = content
                    elif hasattr(content, "text"):
                        # Single TextBlock
                        content_str = content.text
                    else:
                        content_str = str(content)

                    update = StreamUpdate(
                        type="assistant",
                        content=content_str,
                    )
                    await stream_callback(update)

            elif isinstance(message, UserMessage):
                content = getattr(message, "content", "")
                if content:
                    # Handle both string and list content types
                    if isinstance(content, str):
                        content_str = content
                    elif isinstance(content, list):
                        # Extract text from content blocks
                        text_parts = []
                        for block in content:
                            if isinstance(block, TextBlock):
                                text_parts.append(getattr(block, "text", ""))
                            elif hasattr(block, "text"):
                                text_parts.append(block.text)
                            else:
                                text_parts.append(str(block))
                        content_str = "\n".join(text_parts)
                    else:
                        content_str = str(content)

                    update = StreamUpdate(
                        type="user",
                        content=content_str,
                    )
                    await stream_callback(update)

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    def _extract_content_from_messages(self, messages: List[Message]) -> str:
        """Extract content from message list."""
        content_parts = []

        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    # Extract text from TextBlock and ToolResultBlock objects
                    for block in content:
                        if isinstance(block, TextBlock):
                            text = getattr(block, "text", "")
                            if text:
                                content_parts.append(text)
                        elif isinstance(block, ToolResultBlock):
                            # Include tool results in content
                            result_content = getattr(block, "content", "")
                            if result_content:
                                if isinstance(result_content, str):
                                    content_parts.append(result_content)
                                else:
                                    content_parts.append(str(result_content))
                elif content:
                    # Fallback for non-list content
                    content_parts.append(str(content))

        return "\n".join(content_parts)

    def _extract_tools_from_messages(
        self, messages: List[Message]
    ) -> List[Dict[str, Any]]:
        """Extract tools used from message list."""
        tools_used = []
        current_time = asyncio.get_event_loop().time()

        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            tools_used.append(
                                {
                                    "name": getattr(block, "name", "unknown"),
                                    "timestamp": current_time,
                                    "input": getattr(block, "input", {}),
                                }
                            )

        return tools_used

    def _update_session(self, session_id: str, messages: List[Message]) -> None:
        """Update session data."""
        if session_id not in self.active_sessions:
            self.active_sessions[session_id] = {
                "messages": [],
                "created_at": asyncio.get_event_loop().time(),
            }

        session_data = self.active_sessions[session_id]
        session_data["messages"] = messages
        session_data["last_used"] = asyncio.get_event_loop().time()

    async def kill_all_processes(self) -> None:
        """Kill all active processes (no-op for SDK)."""
        logger.info("Clearing active SDK sessions", count=len(self.active_sessions))
        self.active_sessions.clear()

    def get_active_process_count(self) -> int:
        """Get number of active sessions."""
        return len(self.active_sessions)
