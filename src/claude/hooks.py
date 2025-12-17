"""Security hooks for Claude Agent SDK.

Replaces ToolMonitor with SDK-native hook system for better integration.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from opentelemetry import trace

from ..config.settings import Settings
from ..security.validators import SecurityValidator

logger = structlog.get_logger()
tracer = trace.get_tracer("claude.hooks")


class SecurityHooks:
    """Security validation hooks for Claude Agent SDK."""

    def __init__(
        self,
        config: Settings,
        working_directory: Path,
        security_validator: Optional[SecurityValidator] = None,
    ):
        """Initialize security hooks.

        Args:
            config: Application settings
            working_directory: Base working directory for file operations
            security_validator: Optional security validator instance
        """
        self.config = config
        self.working_directory = working_directory
        self.security_validator = security_validator or SecurityValidator(
            approved_directory=config.approved_directory
        )
        self.allowed_tools = config.claude_allowed_tools
        self.disallowed_tools = config.claude_disallowed_tools

    @tracer.start_as_current_span("claude.hook.pre_tool_use")
    async def pre_tool_use_hook(
        self,
        input_data: Dict[str, Any],
        tool_use_id: str,
        context: Any,
    ) -> Dict[str, Any]:
        """Validate tool calls before execution (PreToolUse hook).

        Args:
            input_data: Tool call data with tool_name and tool_input
            tool_use_id: Unique ID for this tool use
            context: Additional context (not used currently)

        Returns:
            Hook response dict with permission decision
        """
        span = trace.get_current_span()
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.use_id", tool_use_id)

        logger.debug(
            "Validating tool call via hook",
            tool_name=tool_name,
            tool_use_id=tool_use_id,
        )

        # Check allowed tools list
        if self.allowed_tools and tool_name not in self.allowed_tools:
            error_msg = f"Tool '{tool_name}' is not in the allowed tools list"
            span.set_attribute("tool.validated", False)
            span.set_attribute("tool.error", "not_in_allowed_list")
            logger.warning(
                "Tool not in allowed list",
                tool_name=tool_name,
                allowed_tools=self.allowed_tools,
            )
            return self._deny(error_msg)

        # Check disallowed tools list
        if self.disallowed_tools and tool_name in self.disallowed_tools:
            error_msg = f"Tool '{tool_name}' is explicitly disallowed"
            span.set_attribute("tool.validated", False)
            span.set_attribute("tool.error", "explicitly_disallowed")
            logger.warning("Tool explicitly disallowed", tool_name=tool_name)
            return self._deny(error_msg)

        # Validate file operations
        if tool_name in ["Read", "Write", "Edit", "create_file", "edit_file", "read_file"]:
            file_path = tool_input.get("file_path") or tool_input.get("path")

            if not file_path:
                error_msg = "File path is required for file operations"
                span.set_attribute("tool.validated", False)
                span.set_attribute("tool.error", "file_path_required")
                logger.warning("File path missing in tool call", tool_name=tool_name)
                return self._deny(error_msg)

            # Validate path security (check for path traversal, etc.)
            valid, resolved_path, error = self.security_validator.validate_path(
                file_path, self.working_directory
            )

            if not valid:
                span.set_attribute("tool.validated", False)
                span.set_attribute("tool.error", "invalid_file_path")
                logger.warning(
                    "Invalid file path in tool call",
                    tool_name=tool_name,
                    file_path=file_path,
                    error=error,
                )
                return self._deny(error or "Invalid file path")

        # Validate bash commands
        if tool_name in ["Bash", "bash", "shell"]:
            command = tool_input.get("command", "")

            if not command:
                error_msg = "Command is required for bash tool"
                span.set_attribute("tool.validated", False)
                span.set_attribute("tool.error", "command_required")
                return self._deny(error_msg)

            # Check for dangerous command patterns
            # Only block truly dangerous patterns that could harm the system
            # Common tools like curl, wget, pipes are allowed for legitimate development work
            dangerous_patterns = [
                "sudo",  # Privilege escalation (per user request)
                "rm -rf /",  # Recursive delete of root
                "chmod 777 /",  # Overly permissive permissions on root
                "mkfs",  # Format filesystem
                "dd if=",  # Disk operations
                "> /dev/sda",  # Write to disk device
                ":(){ :|:& };:",  # Fork bomb
            ]

            command_lower = command.lower()
            for pattern in dangerous_patterns:
                if pattern in command_lower:
                    error_msg = f"Dangerous command pattern detected: {pattern}"
                    span.set_attribute("tool.validated", False)
                    span.set_attribute("tool.error", "dangerous_command")
                    span.set_attribute("tool.dangerous_pattern", pattern)
                    logger.warning(
                        "Dangerous command detected",
                        tool_name=tool_name,
                        command=command,
                        pattern=pattern,
                    )
                    return self._deny(error_msg)

        # All checks passed - approve
        span.set_attribute("tool.validated", True)
        span.set_attribute("tool.approved", True)
        logger.info(
            "Tool call approved",
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            tool_input_size=len(str(tool_input)),
        )
        return {}  # Empty dict means approve

    def _deny(self, reason: str) -> Dict[str, Any]:
        """Create a deny hook response.

        Args:
            reason: Reason for denying the tool use

        Returns:
            Hook response dict with deny decision
        """
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    def create_hooks_config(self) -> Dict[str, List]:
        """Create hooks configuration for ClaudeAgentOptions.

        Returns:
            Dict mapping hook event names to hook handlers
        """
        # Import here to avoid circular dependency
        from claude_agent_sdk import HookMatcher

        return {
            "PreToolUse": [
                HookMatcher(
                    matcher="*",  # Match all tools
                    hooks=[self.pre_tool_use_hook],
                )
            ]
        }
