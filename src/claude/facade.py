"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..config.settings import Settings
from ..utils import ensure_utc
from .cursor_agent_integration import CursorAgentManager
from .exceptions import ClaudeToolValidationError
from .integration import ClaudeProcessManager, ClaudeResponse, StreamUpdate
from .monitor import ToolMonitor
from .sdk_integration import ClaudeSDKManager
from .session import SessionManager

logger = structlog.get_logger()
tracer = trace.get_tracer("claude.facade")

# Type alias for manager types
AgentManager = Union[CursorAgentManager, ClaudeSDKManager, ClaudeProcessManager]


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        process_manager: Optional[ClaudeProcessManager] = None,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        cursor_agent_manager: Optional[CursorAgentManager] = None,
        session_manager: Optional[SessionManager] = None,
        tool_monitor: Optional[ToolMonitor] = None,
    ):
        """Initialize Claude integration facade.

        Agent selection is based on configuration (no fallback):
        1. use_cursor_agent=True -> CursorAgentManager
        2. use_sdk=True -> ClaudeSDKManager
        3. Default -> ClaudeProcessManager
        """
        self.config = config

        # Store all managers for reference
        self.cursor_agent_manager: Optional[CursorAgentManager] = None
        self.sdk_manager: Optional[ClaudeSDKManager] = None
        self.process_manager: Optional[ClaudeProcessManager] = None

        # Initialize manager based on configuration (NO FALLBACK)
        if getattr(config, "use_cursor_agent", False):
            self.cursor_agent_manager = cursor_agent_manager or CursorAgentManager(
                config
            )
            self.manager: AgentManager = self.cursor_agent_manager
            self._agent_type = "cursor-agent"
            logger.info("Using cursor-agent for AI integration")
        elif config.use_sdk:
            self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
            self.manager = self.sdk_manager
            self._agent_type = "claude-sdk"
            logger.info("Using Claude SDK for AI integration")
        else:
            self.process_manager = process_manager or ClaudeProcessManager(config)
            self.manager = self.process_manager
            self._agent_type = "claude-cli"
            logger.info("Using Claude CLI subprocess for AI integration")

        self.session_manager = session_manager
        self.tool_monitor = tool_monitor

        # Track active tasks per user to allow cancellation
        self.active_tasks: Dict[int, asyncio.Task] = {}

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration."""
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
        )

        # Cancel previous task for this user if exists
        if user_id in self.active_tasks:
            previous_task = self.active_tasks[user_id]
            if not previous_task.done():
                logger.info(
                    "Cancelling previous task for user",
                    user_id=user_id,
                    task_done=previous_task.done(),
                )
                previous_task.cancel()
                try:
                    await previous_task
                except asyncio.CancelledError:
                    logger.debug("Previous task cancelled successfully", user_id=user_id)
                except Exception as e:
                    logger.warning(
                        "Error while cancelling previous task",
                        user_id=user_id,
                        error=str(e),
                    )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Track streaming updates and validate tool calls
        tools_validated = True
        validation_errors = []
        blocked_tools = set()

        async def stream_handler(update: StreamUpdate):
            nonlocal tools_validated

            # Validate tool calls
            if update.tool_calls:
                for tool_call in update.tool_calls:
                    tool_name = tool_call["name"]
                    valid, error = await self.tool_monitor.validate_tool_call(
                        tool_name,
                        tool_call.get("input", {}),
                        working_directory,
                        user_id,
                    )

                    if not valid:
                        tools_validated = False
                        validation_errors.append(error)

                        # Track blocked tools
                        if "Tool not allowed:" in error:
                            blocked_tools.add(tool_name)

                        logger.error(
                            "Tool validation failed",
                            tool_name=tool_name,
                            error=error,
                            user_id=user_id,
                        )

                        # For critical tools, we should fail fast
                        if tool_name in ["Task", "Read", "Write", "Edit"]:
                            # Create comprehensive error message
                            admin_instructions = self._get_admin_instructions(
                                list(blocked_tools)
                            )
                            error_msg = self._create_tool_error_message(
                                list(blocked_tools),
                                self.config.claude_allowed_tools or [],
                                admin_instructions,
                            )

                            raise ClaudeToolValidationError(
                                error_msg,
                                blocked_tools=list(blocked_tools),
                                allowed_tools=self.config.claude_allowed_tools or [],
                            )

            # Pass to caller's handler
            if on_stream:
                try:
                    await on_stream(update)
                except Exception as e:
                    logger.warning("Stream callback failed", error=str(e))

        tracer_span = tracer.start_as_current_span("claude.run_command")
        with tracer_span as span:
            span.set_attribute("user_id", user_id)
            span.set_attribute("working_directory", str(working_directory))
            span.set_attribute("has_session_id", bool(session_id))
            span.set_attribute("prompt_length", len(prompt))
            span.set_attribute("agent_type", self._agent_type)

            try:
                # Only continue session if it's not a new session
                should_continue = bool(session_id) and not getattr(
                    session, "is_new_session", False
                )

                # For new sessions, do not pass the temporary session_id to Claude Code
                claude_session_id = (
                    None
                    if getattr(session, "is_new_session", False)
                    else session.session_id
                )

                # Create task for execution to allow cancellation
                async def execute_wrapper():
                    return await self._execute(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=claude_session_id,
                        continue_session=should_continue,
                        stream_callback=stream_handler,
                    )

                task = asyncio.create_task(execute_wrapper())
                self.active_tasks[user_id] = task

                try:
                    response = await task
                except asyncio.CancelledError:
                    logger.info("Task cancelled", user_id=user_id)
                    # Try to kill active processes for this user
                    try:
                        await self.manager.kill_all_processes()
                    except Exception as kill_error:
                        logger.warning(
                            "Failed to kill processes after cancellation",
                            user_id=user_id,
                            error=str(kill_error),
                        )
                    raise
                finally:
                    # Clean up task tracking
                    if user_id in self.active_tasks and self.active_tasks[user_id] == task:
                        del self.active_tasks[user_id]

                # Check if tool validation failed
                if not tools_validated:
                    logger.error(
                        "Command completed but tool validation failed",
                        validation_errors=validation_errors,
                    )
                    # Mark response as having errors and include validation details
                    response.is_error = True
                    response.error_type = "tool_validation_failed"

                    # Extract blocked tool names for user feedback
                    blocked_tools_list = []
                    for error in validation_errors:
                        if "Tool not allowed:" in error:
                            tool_name = error.split("Tool not allowed: ")[1]
                            blocked_tools_list.append(tool_name)

                    # Create user-friendly error message
                    if blocked_tools_list:
                        tool_list = ", ".join(
                            f"`{tool}`" for tool in blocked_tools_list
                        )
                        response.content = (
                            f"🚫 **Tool Access Blocked**\n\n"
                            f"Claude tried to use tools not allowed:\n"
                            f"{tool_list}\n\n"
                            f"**What you can do:**\n"
                            f"• Contact the administrator to request access to these tools\n"
                            f"• Try rephrasing your request to use different approaches\n"
                            f"• Check what tools are currently available with `/status`\n\n"
                            f"**Currently allowed tools:**\n"
                            f"{', '.join(f'`{t}`' for t in self.config.claude_allowed_tools or [])}"
                        )
                    else:
                        response.content = (
                            f"🚫 **Tool Validation Failed**\n\n"
                            f"Tools failed security validation. Try different approach.\n\n"
                            f"Details: {'; '.join(validation_errors)}"
                        )

                # Update session (this may change the session_id for new sessions)
                old_session_id = session.session_id
                await self.session_manager.update_session(session.session_id, response)

                # For new sessions, get the updated session_id from the session manager
                if hasattr(session, "is_new_session") and response.session_id:
                    # The session_id has been updated to Claude's session_id
                    final_session_id = response.session_id
                else:
                    # Use the original session_id for continuing sessions
                    final_session_id = old_session_id

                # Ensure response has the correct session_id
                response.session_id = final_session_id

                # Add result attributes to span
                span.set_attribute("claude.session_id", response.session_id or "")
                span.set_attribute("claude.cost_usd", response.cost)
                span.set_attribute("claude.duration_ms", response.duration_ms)
                span.set_attribute("claude.num_turns", response.num_turns)
                span.set_attribute("claude.is_error", response.is_error)
                span.set_attribute(
                    "claude.tools_used",
                    ",".join(t.get("name", "") for t in (response.tools_used or [])),
                )
                span.set_attribute(
                    "claude.response_length", len(response.content or "")
                )

                logger.info(
                    "Claude command completed",
                    session_id=response.session_id,
                    cost=response.cost,
                    duration_ms=response.duration_ms,
                    num_turns=response.num_turns,
                    is_error=response.is_error,
                )

                if response.is_error:
                    span.set_status(
                        Status(
                            StatusCode.ERROR,
                            description=response.error_type or "unknown_error",
                        )
                    )

                return response

            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, description=str(e)))
                logger.exception(
                    "Claude command failed",
                    user_id=user_id,
                    session_id=session.session_id,
                )
                raise

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
    ) -> ClaudeResponse:
        """Execute command using configured agent (no fallback)."""
        logger.debug(
            "Executing with agent",
            agent_type=self._agent_type,
            working_directory=str(working_directory),
        )

        return await self.manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
        )

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude temporary sessions)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and not s.session_id.startswith("temp_")
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent (use ensure_utc for comparison of mixed naive/aware)
        latest_session = max(matching_sessions, key=lambda s: ensure_utc(s.last_used))

        # Continue session
        return await self.run_command(
            prompt=prompt or "",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session information."""
        return await self.session_manager.get_session_info(session_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_tool_stats(self) -> Dict[str, Any]:
        """Get tool usage statistics."""
        return self.tool_monitor.get_tool_stats()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)
        tool_usage = self.tool_monitor.get_user_tool_usage(user_id)

        return {
            "user_id": user_id,
            **session_summary,
            **tool_usage,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration", agent_type=self._agent_type)

        # Kill any active processes
        await self.manager.kill_all_processes()

        # Clean up expired sessions
        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")

    def get_agent_type(self) -> str:
        """Get the current agent type."""
        return self._agent_type

    def _get_admin_instructions(self, blocked_tools: List[str]) -> str:
        """Generate admin instructions for enabling blocked tools."""
        instructions = []

        # Check if settings file exists
        settings_file = Path(".env")

        if blocked_tools:
            # Get current allowed tools and create merged list without duplicates
            current_tools = [
                "Read",
                "Write",
                "Edit",
                "Bash",
                "Glob",
                "Grep",
                "LS",
                "Task",
                "MultiEdit",
                "NotebookRead",
                "NotebookEdit",
                "WebFetch",
                "TodoRead",
                "TodoWrite",
                "WebSearch",
            ]
            merged_tools = list(
                dict.fromkeys(current_tools + blocked_tools)
            )  # Remove duplicates while preserving order
            merged_tools_str = ",".join(merged_tools)
            merged_tools_py = ", ".join(f'"{tool}"' for tool in merged_tools)

            instructions.append("**For Administrators:**")
            instructions.append("")

            if settings_file.exists():
                instructions.append(
                    "To enable these tools, add them to your `.env` file:"
                )
                instructions.append("```")
                instructions.append(f'CLAUDE_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")
            else:
                instructions.append("To enable these tools:")
                instructions.append("1. Create a `.env` file in your project root")
                instructions.append("2. Add the following line:")
                instructions.append("```")
                instructions.append(f'CLAUDE_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")

            instructions.append("")
            instructions.append("Or modify the default in `src/config/settings.py`:")
            instructions.append("```python")
            instructions.append("claude_allowed_tools: Optional[List[str]] = Field(")
            instructions.append(f"    default=[{merged_tools_py}],")
            instructions.append('    description="List of allowed Claude tools",')
            instructions.append(")")
            instructions.append("```")

        return "\n".join(instructions)

    def _create_tool_error_message(
        self,
        blocked_tools: List[str],
        allowed_tools: List[str],
        admin_instructions: str,
    ) -> str:
        """Create a comprehensive error message for tool validation failures."""
        tool_list = ", ".join(f"`{tool}`" for tool in blocked_tools)
        allowed_list = (
            ", ".join(f"`{tool}`" for tool in allowed_tools)
            if allowed_tools
            else "None"
        )

        message = [
            "🚫 **Tool Access Blocked**",
            "",
            f"Claude tried to use tools that are not currently allowed:",
            f"{tool_list}",
            "",
            "**Why this happened:**",
            "• Claude needs these tools to complete your request",
            "• These tools are not in the allowed tools list",
            "• This is a security feature to control what Claude can do",
            "",
            "**What you can do:**",
            "• Contact the administrator to request access to these tools",
            "• Try rephrasing your request to use different approaches",
            "• Use simpler requests that don't require these tools",
            "",
            "**Currently allowed tools:**",
            f"{allowed_list}",
            "",
            admin_instructions,
        ]

        return "\n".join(message)
