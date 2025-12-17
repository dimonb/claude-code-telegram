"""Monitor Claude's tool usage.

Features:
- Track tool calls
- Security validation
- Usage analytics
"""

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
from opentelemetry import trace

from ..config.settings import Settings
from ..security.validators import SecurityValidator

logger = structlog.get_logger()
tracer = trace.get_tracer("claude.monitor")


class ToolMonitor:
    """Monitor and validate Claude's tool usage."""

    def __init__(
        self, config: Settings, security_validator: Optional[SecurityValidator] = None
    ):
        """Initialize tool monitor."""
        self.config = config
        self.security_validator = security_validator
        self.tool_usage: Dict[str, int] = defaultdict(int)
        self.security_violations: List[Dict[str, Any]] = []

    @tracer.start_as_current_span("claude.tool.validate")
    async def validate_tool_call(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        working_directory: Path,
        user_id: int,
    ) -> Tuple[bool, Optional[str]]:
        """Validate tool call before execution."""
        span = trace.get_current_span()
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("user_id", user_id)
        span.set_attribute("working_directory", str(working_directory))

        logger.debug(
            "Validating tool call",
            tool_name=tool_name,
            working_directory=str(working_directory),
            user_id=user_id,
        )

        # Check if tool is allowed
        if (
            hasattr(self.config, "claude_allowed_tools")
            and self.config.claude_allowed_tools
        ):
            if tool_name not in self.config.claude_allowed_tools:
                violation = {
                    "type": "disallowed_tool",
                    "tool_name": tool_name,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                }
                self.security_violations.append(violation)
                span.set_attribute("tool.validated", False)
                span.set_attribute("tool.error", "disallowed_tool")
                logger.warning("Tool not allowed", **violation)
                return False, f"Tool not allowed: {tool_name}"

        # Check if tool is explicitly disallowed
        if (
            hasattr(self.config, "claude_disallowed_tools")
            and self.config.claude_disallowed_tools
        ):
            if tool_name in self.config.claude_disallowed_tools:
                violation = {
                    "type": "explicitly_disallowed_tool",
                    "tool_name": tool_name,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                }
                self.security_violations.append(violation)
                span.set_attribute("tool.validated", False)
                span.set_attribute("tool.error", "explicitly_disallowed")
                logger.warning("Tool explicitly disallowed", **violation)
                return False, f"Tool explicitly disallowed: {tool_name}"

        # Validate file operations
        if tool_name in [
            "create_file",
            "edit_file",
            "read_file",
            "Write",
            "Edit",
            "Read",
        ]:
            file_path = tool_input.get("path") or tool_input.get("file_path")
            if not file_path:
                span.set_attribute("tool.validated", False)
                span.set_attribute("tool.error", "file_path_required")
                return False, "File path required"

            # Validate path security
            if self.security_validator:
                valid, resolved_path, error = self.security_validator.validate_path(
                    file_path, working_directory
                )

                if not valid:
                    violation = {
                        "type": "invalid_file_path",
                        "tool_name": tool_name,
                        "file_path": file_path,
                        "user_id": user_id,
                        "working_directory": str(working_directory),
                        "error": error,
                    }
                    self.security_violations.append(violation)
                    span.set_attribute("tool.validated", False)
                    span.set_attribute("tool.error", "invalid_file_path")
                    logger.warning("Invalid file path in tool call", **violation)
                    return False, error

        # Validate shell commands
        # NOTE: This validation is secondary to SecurityHooks in the SDK
        # Only block truly dangerous patterns, not common shell operators
        if tool_name in ["bash", "shell", "Bash"]:
            command = tool_input.get("command", "")

            # Check for truly dangerous commands only
            # Common shell operators like >, |, ;, &, $() are allowed
            dangerous_patterns = [
                "sudo",      # Privilege escalation (per user request)
                "rm -rf /",  # Recursive delete of root
                "chmod 777", # Overly permissive chmod
                # Note: curl, wget, pipes, redirects are allowed for legitimate use
            ]

            for pattern in dangerous_patterns:
                if pattern in command.lower():
                    violation = {
                        "type": "dangerous_command",
                        "tool_name": tool_name,
                        "command": command,
                        "pattern": pattern,
                        "user_id": user_id,
                        "working_directory": str(working_directory),
                    }
                    self.security_violations.append(violation)
                    span.set_attribute("tool.validated", False)
                    span.set_attribute("tool.error", "dangerous_command")
                    logger.warning("Dangerous command detected", **violation)
                    return False, f"Dangerous command pattern detected: {pattern}"

        # Track usage
        self.tool_usage[tool_name] += 1

        span.set_attribute("tool.validated", True)
        logger.debug("Tool call validated successfully", tool_name=tool_name)
        return True, None

    def get_tool_stats(self) -> Dict[str, Any]:
        """Get tool usage statistics."""
        return {
            "total_calls": sum(self.tool_usage.values()),
            "by_tool": dict(self.tool_usage),
            "unique_tools": len(self.tool_usage),
            "security_violations": len(self.security_violations),
        }

    def get_security_violations(self) -> List[Dict[str, Any]]:
        """Get security violations."""
        return self.security_violations.copy()

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.tool_usage.clear()
        self.security_violations.clear()
        logger.info("Tool monitor statistics reset")

    def get_user_tool_usage(self, user_id: int) -> Dict[str, Any]:
        """Get tool usage for specific user."""
        user_violations = [
            v for v in self.security_violations if v.get("user_id") == user_id
        ]

        return {
            "user_id": user_id,
            "security_violations": len(user_violations),
            "violation_types": list(set(v.get("type") for v in user_violations)),
        }

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if tool is allowed without validation."""
        # Check allowed list
        if (
            hasattr(self.config, "claude_allowed_tools")
            and self.config.claude_allowed_tools
        ):
            if tool_name not in self.config.claude_allowed_tools:
                return False

        # Check disallowed list
        if (
            hasattr(self.config, "claude_disallowed_tools")
            and self.config.claude_disallowed_tools
        ):
            if tool_name in self.config.claude_disallowed_tools:
                return False

        return True
