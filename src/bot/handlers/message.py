"""Message handlers for non-command inputs."""

import asyncio
import json
import time
from typing import Callable, Optional

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from telegram import Update
from telegram.ext import ContextTypes

from ...claude.exceptions import ClaudeToolValidationError
from ...claude.integration import StreamUpdate
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.rate_limiter import RateLimiter
from ...security.validators import SecurityValidator

logger = structlog.get_logger()
tracer = trace.get_tracer("telegram.handlers")

TODO_CHECKBOX = {
    "TODO_STATUS_PENDING": "⬜️",
    "TODO_STATUS_IN_PROGRESS": "⏳",
    "TODO_STATUS_COMPLETED": "✅",
    "TODO_STATUS_BLOCKED": "⚠️",
}

TODO_LABEL = {
    "TODO_STATUS_PENDING": "pending",
    "TODO_STATUS_IN_PROGRESS": "in progress",
    "TODO_STATUS_COMPLETED": "completed",
    "TODO_STATUS_BLOCKED": "blocked",
}


def _format_tool_name(tool_name: str) -> str:
    """Format tool name for display."""
    # Handle MCP tools: mcp_provider_toolname -> Provider:ToolName
    if tool_name.startswith("mcp_"):
        parts = tool_name.split("_", 2)
        if len(parts) >= 3:
            provider = parts[1].replace("-", " ").title()
            name = parts[2].replace("_", " ").replace("-", " ").title()
            return f"{provider}:{name}"

    # Regular tools: capitalize
    return tool_name.replace("_", " ").title()


def _escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown parse mode."""
    escape_chars = r"\_*`["
    for ch in escape_chars:
        text = text.replace(ch, f"\\{ch}")
    return text


def _format_tool_params(params: dict, max_length: int = 50) -> str:
    """Format tool parameters compactly."""
    if not params:
        return "()"

    # Format key-value pairs
    parts = []
    for key, value in params.items():
        # Truncate long values
        if isinstance(value, str):
            if len(value) > 30:
                value = value[:30] + "..."
            parts.append(f'{key}="{value}"')
        elif isinstance(value, (int, float, bool)) or value is None:
            parts.append(f"{key}={value}")
        else:
            # For complex types, try to serialize to JSON for readability
            try:
                serialized = json.dumps(value)
            except Exception:
                serialized = str(value) if value is not None else "null"

            if len(serialized) > 30:
                serialized = serialized[:30] + "..."

            parts.append(f"{key}={serialized}")

    params_str = ", ".join(parts)
    if len(params_str) > max_length:
        params_str = params_str[:max_length] + "..."

    return f"({params_str})"


def _format_progress_update(
    update_obj: "StreamUpdate", tool_journal: dict = None, tool_order: list = None
) -> Optional[str]:
    """Format progress updates with enhanced context and visual indicators."""
    # Build tool journal section if we have tools
    journal_lines = []
    if tool_journal and tool_order:
        logger.debug(
            "Formatting progress with tool journal",
            update_type=update_obj.type,
            journal_size=len(tool_journal),
            tools_count=len(tool_order),
        )
        for call_id in tool_order:
            if call_id in tool_journal:
                entry = tool_journal[call_id]
                tool_name = _format_tool_name(entry["name"])
                params_str = _format_tool_params(entry["params"])
                icon = entry["icon"]
                status_text = entry.get("status")
                status_suffix = f" [{status_text}]" if status_text else ""
                # Escape Markdown special characters to prevent parsing errors
                escaped_tool_name = _escape_markdown(tool_name)
                escaped_params = _escape_markdown(params_str)
                escaped_status = _escape_markdown(status_suffix)
                journal_lines.append(
                    f"{icon} {escaped_tool_name}{escaped_params}{escaped_status}"
                )

    # Format the journal
    journal_text = ""
    if journal_lines:
        journal_text = "\n".join(journal_lines) + "\n\n"

    if update_obj.type == "tool_result":
        # Show tool completion status
        tool_name = None

        # Try to extract tool name from metadata
        if update_obj.metadata:
            tool_name = update_obj.metadata.get("tool_name")

        # Try to extract from tool_calls if metadata doesn't have it
        if not tool_name or tool_name == "unknown":
            if update_obj.tool_calls and len(update_obj.tool_calls) > 0:
                tool_name = update_obj.tool_calls[0].get("name")

        # Fallback to "Tool" if still unknown
        if not tool_name or tool_name == "unknown":
            tool_name = "Tool"

        # For tool_result, just return the journal (status already updated in journal)
        if journal_text:
            return journal_text + "🤔 Processing..."

        # Fallback if no journal
        display_name = tool_name.capitalize() if tool_name else "Tool"
        escaped_display_name = _escape_markdown(display_name)
        if update_obj.is_error():
            error_msg = _escape_markdown(
                update_obj.get_error_message() or "Unknown error"
            )
            return f"❌ **{escaped_display_name} failed**\n\n_{error_msg}_"
        else:
            return f"✅ **{escaped_display_name} completed**"

    elif update_obj.type == "progress":
        # Handle progress updates
        content = _escape_markdown(update_obj.content or "Working...")
        progress_text = f"🔄 **{content}**"

        percentage = update_obj.get_progress_percentage()
        if percentage is not None:
            # Create a simple progress bar
            filled = int(percentage / 10)  # 0-10 scale
            bar = "█" * filled + "░" * (10 - filled)
            progress_text += f"\n\n`{bar}` {percentage}%"

        if update_obj.progress:
            step = update_obj.progress.get("step")
            total_steps = update_obj.progress.get("total_steps")
            if step and total_steps:
                progress_text += f"\n\nStep {step} of {total_steps}"

        return journal_text + progress_text

    elif update_obj.type == "error":
        # Handle error messages
        error_msg = _escape_markdown(update_obj.get_error_message() or "Unknown error")
        return journal_text + f"❌ **Error**\n\n_{error_msg}_"

    elif update_obj.type == "assistant" and update_obj.tool_calls:
        # Tool calls are already shown in journal, just show working status
        return journal_text + "🤔 Processing..."

    elif update_obj.type == "assistant" and update_obj.content:
        # Regular content updates with preview
        content_preview = (
            update_obj.content[:150] + "..."
            if len(update_obj.content) > 150
            else update_obj.content
        )
        escaped_preview = _escape_markdown(content_preview)
        return journal_text + f"🤖 **Claude is working...**\n\n_{escaped_preview}_"

    elif update_obj.type == "system":
        # System initialization or other system messages
        if update_obj.metadata and update_obj.metadata.get("subtype") == "init":
            tools_count = len(update_obj.metadata.get("tools", []))
            model = _escape_markdown(update_obj.metadata.get("model", "Claude"))
            return f"🚀 **Starting {model}** with {tools_count} tools available"

    elif update_obj.type == "thinking":
        # Handle thinking updates (from cursor-agent)
        subtype = update_obj.metadata.get("subtype") if update_obj.metadata else None
        if subtype == "delta":
            # For delta updates, show static thinking indicator (no content to avoid flicker)
            return None  # Skip delta updates to prevent flickering
        elif subtype == "completed":
            # Show journal with thinking
            return journal_text + "💭 **Thinking...**"

    elif update_obj.type == "tool_call":
        # Tool started - show journal with running status
        return journal_text + "🤔 Processing..."

    # Default: just show journal if we have it
    if journal_text:
        return journal_text + "🤔 Processing..."

    return None


def _normalize_todo_payload(todos_payload: object) -> dict:
    """Convert todo payload into a normalized mapping keyed by id."""
    items = []
    if isinstance(todos_payload, list):
        items = [i for i in todos_payload if isinstance(i, dict)]
    elif isinstance(todos_payload, dict):
        todos_list = todos_payload.get("todos") or todos_payload.get("items")
        if isinstance(todos_list, list):
            items = [i for i in todos_list if isinstance(i, dict)]
        elif todos_payload.get("id"):
            items = [todos_payload]

    normalized = {}
    for item in items:
        todo_id = str(item.get("id") or item.get("content") or "")
        if not todo_id:
            continue

        normalized[todo_id] = {
            "id": todo_id,
            "content": item.get("content") or todo_id,
            "status": item.get("status", "TODO_STATUS_PENDING"),
            "dependencies": item.get("dependencies", []),
            "createdAt": item.get("createdAt"),
            "updatedAt": item.get("updatedAt"),
        }

    return normalized


def _render_todo_list(
    todos: dict, escape_func: Optional[Callable[[str], str]] = None
) -> Optional[str]:
    """Render todos as a checkbox list with statuses.

    Optionally apply escape_func to each rendered line for Markdown safety.
    """
    if not todos:
        return None

    heading = "📋 TODO"
    if escape_func:
        heading = escape_func(heading)

    lines = [heading]
    # Preserve insertion order if already ordered; fallback to timestamp/id sort
    values = list(todos.values())
    try:
        from collections.abc import Mapping

        if isinstance(todos, Mapping) and hasattr(todos, "items"):
            ordered_values = list(todos.values())
            if ordered_values:
                values = ordered_values
    except Exception:
        pass

    for todo in values:
        status = todo.get("status", "TODO_STATUS_PENDING")
        checkbox = TODO_CHECKBOX.get(status, "[ ]")
        content = todo.get("content") or todo.get("id")
        is_done = status == "TODO_STATUS_COMPLETED"

        if escape_func:
            checkbox = escape_func(checkbox)
            content = escape_func(content)
            if is_done:
                content = f"~{content}~"
            line = f"\\- {checkbox} {content}"
        else:
            if is_done:
                content = f"~{content}~"
            line = f"- {checkbox} {content}"
        lines.append(line)

    return "\n".join(lines)


def _format_error_message(error_str: str) -> str:
    """Format error messages for user-friendly display."""
    # Check if message is already formatted (contains emoji and markdown)
    if error_str.startswith(("🔄", "⏱️", "⏰", "🚫", "❌")) and "**" in error_str:
        # Already formatted, return as-is
        return error_str

    # Check for limit reached (various formats)
    if "limit reached" in error_str.lower():
        # Usage limit error - already user-friendly if it starts with emoji
        if error_str.startswith("⏱️"):
            return error_str
        # Otherwise format it
        import re

        time_match = re.search(
            r"resets?\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*[apm]{0,2})",
            error_str,
            re.IGNORECASE,
        )
        timezone_match = re.search(r"\(([^)]+)\)", error_str)
        reset_time = time_match.group(1) if time_match else "later"
        timezone = timezone_match.group(1) if timezone_match else ""

        return (
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
    elif "tool not allowed" in error_str.lower():
        # Tool validation error - already handled in facade.py
        return error_str
    elif (
        "no conversation found" in error_str.lower()
        or "session not found" in error_str.lower()
        or "conversation not found" in error_str.lower()
    ):
        return (
            f"🔄 **Session Not Found**\n\n"
            f"The Claude session could not be found or has expired.\n\n"
            f"**What you can do:**\n"
            f"• Use `/new` to start a fresh session\n"
            f"• Try your request again\n"
            f"• Use `/status` to check your current session"
        )
    elif "rate limit" in error_str.lower():
        return (
            f"⏱️ **Rate Limit Reached**\n\n"
            f"Too many requests in a short time period.\n\n"
            f"**What you can do:**\n"
            f"• Wait a moment before trying again\n"
            f"• Use simpler requests\n"
            f"• Check your current usage with `/status`"
        )
    elif "timeout" in error_str.lower():
        return (
            f"⏰ **Request Timeout**\n\n"
            f"Your request took too long to process and timed out.\n\n"
            f"**What you can do:**\n"
            f"• Try breaking down your request into smaller parts\n"
            f"• Use simpler commands\n"
            f"• Try again in a moment"
        )
    else:
        # Generic error handling
        return (
            f"❌ **Claude Code Error**\n\n"
            f"Failed to process your request: {error_str}\n\n"
            f"Please try again or contact the administrator if the problem persists."
        )


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle regular text messages as Claude prompts."""
    user_id = update.effective_user.id
    message_text = update.message.text
    settings: Settings = context.bot_data["settings"]

    # Get services
    rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    claude_response = None

    logger.info(
        "Processing text message", user_id=user_id, message_length=len(message_text)
    )

    with tracer.start_as_current_span("telegram.handle_text") as span:
        progress_msg = None
        try:
            span.set_attribute("telegram.user_id", user_id)
            if update.effective_chat:
                span.set_attribute("telegram.chat_id", update.effective_chat.id)
            span.set_attribute("telegram.message_length", len(message_text))

            current_dir = context.user_data.get(
                "current_directory", settings.approved_directory
            )
            span.set_attribute("working_directory", str(current_dir))

            # Check rate limit with estimated cost for text processing
            estimated_cost = _estimate_text_processing_cost(message_text)

            if rate_limiter:
                allowed, limit_message = await rate_limiter.check_rate_limit(
                    user_id, estimated_cost
                )
                if not allowed:
                    await update.message.reply_text(f"⏱️ {limit_message}")
                    return

            # Send typing indicator
            await update.message.chat.send_action("typing")

            # Create progress message
            progress_msg = await update.message.reply_text(
                "🤔 Processing your request...",
                reply_to_message_id=update.message.message_id,
            )

            # Get Claude integration and storage from context
            claude_integration = context.bot_data.get("claude_integration")
            storage = context.bot_data.get("storage")

            if not claude_integration:
                await update.message.reply_text(
                    "❌ **Claude integration not available**\n\n"
                    "The Claude Code integration is not properly configured. "
                    "Please contact the administrator.",
                    parse_mode="Markdown",
                )
                return

            # Get existing session ID
            session_id = context.user_data.get("claude_session_id")

            # Enhanced stream updates handler with progress tracking and throttling
            last_progress_text = None  # Track last text to avoid duplicate edits
            last_update_time = 0.0  # Throttle updates to prevent flickering
            update_interval = 1.5  # Minimum seconds between updates

            # Tool execution journal: {call_id: {"name": str, "params": dict, "status": str, "icon": str}}
            tool_journal = {}
            # Order of tool calls for display
            tool_order = []

            async def stream_handler(update_obj):
                nonlocal last_progress_text, last_update_time, tool_journal, tool_order
                try:
                    # Check for usage limit in stream content
                    # Handle both string and list content types
                    if isinstance(update_obj.content, str):
                        content_str = update_obj.content
                    elif isinstance(update_obj.content, list):
                        # Handle list of strings or TextBlock objects
                        content_parts = []
                        for item in update_obj.content:
                            if isinstance(item, str):
                                content_parts.append(item)
                            elif hasattr(item, "text"):
                                content_parts.append(item.text)
                            else:
                                content_parts.append(str(item))
                        content_str = " ".join(content_parts)
                    else:
                        content_str = str(update_obj.content)
                    if content_str and "limit reached" in content_str.lower():
                        import re

                        current_span = trace.get_current_span()
                        if current_span.is_recording():
                            content = update_obj.content
                            time_match = re.search(
                                r"resets?\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*[apm]{0,2})",
                                content,
                                re.IGNORECASE,
                            )
                            timezone_match = re.search(r"\(([^)]+)\)", content)

                            reset_time = time_match.group(1) if time_match else "later"
                            timezone = timezone_match.group(1) if timezone_match else ""

                            current_span.set_attribute(
                                "claude.error_type", "usage_limit_reached"
                            )
                            current_span.set_attribute(
                                "claude.limit_reset_time", reset_time
                            )
                            current_span.set_attribute(
                                "claude.limit_timezone", timezone
                            )
                            current_span.set_status(
                                Status(
                                    StatusCode.ERROR, description="usage_limit_reached"
                                )
                            )

                    # Track tool calls in journal
                    if update_obj.type == "tool_call":
                        # Tool started
                        metadata = update_obj.metadata or {}
                        call_id = metadata.get("call_id") or metadata.get("tool_use_id")
                        tool_name = metadata.get("tool_name", "tool")

                        # Extract parameters from tool_calls or metadata fallback
                        params = {}
                        if update_obj.tool_calls and len(update_obj.tool_calls) > 0:
                            tool_call = update_obj.tool_calls[0]
                            tool_name = tool_call.get("name", tool_name)
                            params = tool_call.get("input", {})
                        if not params:
                            params = metadata.get("tool_args", {})

                        if call_id:
                            tool_journal[call_id] = {
                                "name": tool_name,
                                "params": params,
                                "status": "running",
                                "icon": "⏳",
                            }
                            if call_id not in tool_order:
                                tool_order.append(call_id)
                            logger.debug(
                                "Tool started, added to journal",
                                call_id=call_id,
                                tool_name=tool_name,
                                journal_size=len(tool_journal),
                            )

                    elif update_obj.type == "tool_result":
                        # Tool completed
                        metadata = update_obj.metadata or {}
                        call_id = metadata.get("call_id") or metadata.get("tool_use_id")
                        if call_id:
                            existing_entry = tool_journal.get(call_id, {})
                            params = existing_entry.get("params", {})
                            tool_name = existing_entry.get(
                                "name", metadata.get("tool_name", "tool")
                            )

                            # Keep args/name up to date if completion carries details
                            if update_obj.tool_calls and len(update_obj.tool_calls) > 0:
                                tool_call = update_obj.tool_calls[0]
                                tool_name = tool_call.get("name", tool_name)
                                params = tool_call.get("input", params or {})
                            if not params:
                                params = metadata.get("tool_args", params)

                            is_error = (
                                update_obj.is_error()
                                or metadata.get("status") == "error"
                            )

                            tool_journal[call_id] = {
                                "name": tool_name,
                                "params": params,
                                "status": "error" if is_error else "success",
                                "icon": "❌" if is_error else "✅",
                            }

                            # Update session-scoped todos when cursor-agent updateTodos tool completes
                            if tool_name and tool_name.lower() == "updatetodos":
                                session_id_for_todos = (
                                    update_obj.session_context or {}
                                ).get("session_id") or context.user_data.get(
                                    "claude_session_id"
                                )

                                todos_payload = None
                                if (
                                    update_obj.tool_calls
                                    and len(update_obj.tool_calls) > 0
                                ):
                                    tool_call_data = update_obj.tool_calls[0]
                                    todos_payload = tool_call_data.get("result")
                                    if todos_payload is None:
                                        todos_payload = tool_call_data.get("input")
                                if todos_payload is None:
                                    todos_payload = metadata.get("tool_args")

                                normalized_todos = _normalize_todo_payload(
                                    todos_payload
                                )
                                if normalized_todos and session_id_for_todos:
                                    session_todos = context.user_data.setdefault(
                                        "session_todos", {}
                                    )
                                    existing_todos = session_todos.get(
                                        session_id_for_todos, {}
                                    )
                                    merged_todos = {**existing_todos}
                                    for todo_id, todo_data in normalized_todos.items():
                                        merged_todos[todo_id] = {
                                            **existing_todos.get(todo_id, {}),
                                            **todo_data,
                                        }
                                    session_todos[session_id_for_todos] = merged_todos

                            if call_id not in tool_order:
                                tool_order.append(call_id)

                            logger.debug(
                                "Tool completed, updated in journal",
                                call_id=call_id,
                                tool_name=tool_name,
                                is_error=is_error,
                            )

                    progress_text = _format_progress_update(
                        update_obj, tool_journal, tool_order
                    )

                    # Throttle updates to prevent flickering
                    current_time = time.time()
                    time_since_last = current_time - last_update_time

                    # Always update for important events (tool completion, errors)
                    is_important = update_obj.type in ("tool_result", "error")

                    if progress_text and progress_text != last_progress_text:
                        # Update if: important event OR enough time passed
                        if is_important or time_since_last >= update_interval:
                            await progress_msg.edit_text(
                                progress_text, parse_mode="Markdown"
                            )
                            last_progress_text = progress_text
                            last_update_time = current_time
                except Exception as stream_error:
                    logger.exception(
                        "Failed to update progress message", error=str(stream_error)
                    )

            # Run Claude command in background task to avoid blocking handler
            # This allows new messages to be processed and cancel previous tasks
            # Check and cancel previous task for this user BEFORE starting new one
            if hasattr(claude_integration, "active_tasks"):
                if user_id in claude_integration.active_tasks:
                    previous_task = claude_integration.active_tasks[user_id]
                    if not previous_task.done():
                        logger.info(
                            "Cancelling previous task for user before starting new one",
                            user_id=user_id,
                        )
                        previous_task.cancel()
                        # Don't await - let it cancel in background to avoid blocking
                        # The cancellation will be handled in run_command's exception handler

            # Create background task to handle the command
            # This allows the handler to return immediately and process new messages
            async def process_command():
                claude_response = None
                try:
                    claude_response = await claude_integration.run_command(
                        prompt=message_text,
                        working_directory=current_dir,
                        user_id=user_id,
                        session_id=session_id,
                        on_stream=stream_handler,
                    )

                    # Update session ID
                    context.user_data["claude_session_id"] = claude_response.session_id

                    # Check if Claude changed the working directory and update our tracking
                    _update_working_directory_from_claude_response(
                        claude_response, context, settings, user_id
                    )

                    # Log interaction to storage
                    if storage:
                        try:
                            await storage.save_claude_interaction(
                                user_id=user_id,
                                session_id=claude_response.session_id,
                                prompt=message_text,
                                response=claude_response,
                                ip_address=None,  # Telegram doesn't provide IP
                            )
                        except Exception as storage_error:
                            logger.warning(
                                "Failed to log interaction to storage",
                                error=str(storage_error),
                            )

                    # Format response
                    from ..utils.formatting import ResponseFormatter

                    formatter = ResponseFormatter(settings)
                    formatted_messages = formatter.format_claude_response(
                        claude_response.content
                    )

                    todo_text = _render_todo_list(
                        (context.user_data.get("session_todos") or {}).get(
                            claude_response.session_id, {}
                        )
                    )
                    if todo_text and formatted_messages:
                        formatted_messages[-1].text = (
                            f"{formatted_messages[-1].text}\n\n{todo_text}"
                        )

                    # Delete progress message
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass

                    # Send formatted responses (may be multiple messages)
                    for i, message in enumerate(formatted_messages):
                        try:
                            await update.message.reply_text(
                                message.text,
                                parse_mode=message.parse_mode,
                                reply_markup=message.reply_markup,
                                reply_to_message_id=(
                                    update.message.message_id if i == 0 else None
                                ),
                            )

                            # Small delay between messages to avoid rate limits
                            if i < len(formatted_messages) - 1:
                                await asyncio.sleep(0.5)

                        except Exception as send_error:
                            logger.error(
                                "Failed to send response message",
                                error=str(send_error),
                                message_index=i,
                            )
                            # Try to send error message
                            await update.message.reply_text(
                                "❌ Failed to send response. Please try again.",
                                reply_to_message_id=(
                                    update.message.message_id if i == 0 else None
                                ),
                            )

                    # Update session info
                    context.user_data["last_message"] = update.message.text

                    # Add conversation enhancements if available
                    features = context.bot_data.get("features")
                    conversation_enhancer = (
                        features.get_conversation_enhancer() if features else None
                    )

                    if conversation_enhancer and claude_response:
                        try:
                            # Update conversation context
                            conversation_enhancer.update_context(
                                user_id, claude_response
                            )
                            conversation_context = (
                                conversation_enhancer.get_or_create_context(user_id)
                            )

                            # Check if we should show follow-up suggestions
                            if conversation_enhancer.should_show_suggestions(
                                claude_response.tools_used or [],
                                claude_response.content,
                            ):
                                # Generate follow-up suggestions
                                suggestions = conversation_enhancer.generate_follow_up_suggestions(
                                    claude_response.content,
                                    claude_response.tools_used or [],
                                    conversation_context,
                                )

                                if suggestions:
                                    # Create inline keyboard with suggestions
                                    from telegram import (
                                        InlineKeyboardButton,
                                        InlineKeyboardMarkup,
                                    )

                                    keyboard = [
                                        [
                                            InlineKeyboardButton(
                                                suggestion,
                                                callback_data=f"suggestion:{i}",
                                            )
                                        ]
                                        for i, suggestion in enumerate(suggestions[:3])
                                    ]
                                    reply_markup = InlineKeyboardMarkup(keyboard)

                                    await update.message.reply_text(
                                        "💡 **Follow-up suggestions:**",
                                        reply_markup=reply_markup,
                                        parse_mode="Markdown",
                                    )
                        except Exception as enhancer_error:
                            logger.warning(
                                "Failed to add conversation enhancements",
                                error=str(enhancer_error),
                            )

                except asyncio.CancelledError:
                    # Task was cancelled due to new message from same user
                    logger.info(
                        "Claude command cancelled due to new message",
                        user_id=user_id,
                    )
                    # Delete progress message and return early - new message will be processed
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass
                    return
                except ClaudeToolValidationError as e:
                    # Tool validation error with detailed instructions
                    logger.error(
                        "Tool validation error",
                        error=str(e),
                        user_id=user_id,
                        blocked_tools=e.blocked_tools,
                    )
                    # Error message already formatted, create FormattedMessage
                    from ..utils.formatting import FormattedMessage

                    formatted_messages = [
                        FormattedMessage(str(e), parse_mode="Markdown")
                    ]

                    # Delete progress message
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass

                    # Send error message
                    await update.message.reply_text(
                        formatted_messages[0].text,
                        parse_mode=formatted_messages[0].parse_mode,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as e:
                    logger.exception(
                        "Claude integration failed", error=str(e), user_id=user_id
                    )
                    # Format error and create FormattedMessage
                    from ..utils.formatting import FormattedMessage

                    formatted_messages = [
                        FormattedMessage(
                            _format_error_message(str(e)), parse_mode="Markdown"
                        )
                    ]

                    # Delete progress message
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass

                    # Send error message
                    await update.message.reply_text(
                        formatted_messages[0].text,
                        parse_mode=formatted_messages[0].parse_mode,
                        reply_to_message_id=update.message.message_id,
                    )

            # Start background task and return immediately
            # This allows the handler to process new messages
            task = asyncio.create_task(process_command())

            # Store task in context for potential cancellation
            if "background_tasks" not in context.user_data:
                context.user_data["background_tasks"] = []
            context.user_data["background_tasks"].append(task)

            # Clean up completed tasks
            context.user_data["background_tasks"] = [
                t for t in context.user_data["background_tasks"] if not t.done()
            ]

            # Return immediately - task will handle response
            return

        except Exception as e:
            # Clean up progress message if it exists
            try:
                if progress_msg is not None:
                    await progress_msg.delete()
            except Exception:
                logger.warning(
                    "Failed to delete progress message during error handling",
                    user_id=user_id,
                )

            if span.is_recording():
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))

            error_msg = f"❌ **Error processing message**\n\n{str(e)}"
            await update.message.reply_text(error_msg, parse_mode="Markdown")

            # Log failed processing
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="text_message",
                    args=[update.message.text[:100]],
                    success=False,
                )

            logger.exception(
                "Error processing text message", error=str(e), user_id=user_id
            )

            # Only try conversation enhancement if variables are defined
            features = context.bot_data.get("features")
            conversation_enhancer = (
                features.get_conversation_enhancer() if features else None
            )

            if conversation_enhancer and claude_response:
                try:
                    # Update conversation context
                    conversation_enhancer.update_context(user_id, claude_response)
                    conversation_context = conversation_enhancer.get_or_create_context(
                        user_id
                    )

                    # Check if we should show follow-up suggestions
                    if conversation_enhancer.should_show_suggestions(
                        claude_response.tools_used or [], claude_response.content
                    ):
                        # Generate follow-up suggestions
                        suggestions = (
                            conversation_enhancer.generate_follow_up_suggestions(
                                claude_response.content,
                                claude_response.tools_used or [],
                                conversation_context,
                            )
                        )

                        if suggestions:
                            # Create keyboard with suggestions
                            suggestion_keyboard = (
                                conversation_enhancer.create_follow_up_keyboard(
                                    suggestions
                                )
                            )

                            # Send follow-up suggestions
                            await update.message.reply_text(
                                "💡 **What would you like to do next?**",
                                parse_mode="Markdown",
                                reply_markup=suggestion_keyboard,
                            )

                except Exception as conv_error:
                    logger.warning(
                        "Conversation enhancement failed",
                        error=str(conv_error),
                        user_id=user_id,
                    )

        # No outer exception handler here; errors are handled above per-case.


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads."""
    user_id = update.effective_user.id
    document = update.message.document
    settings: Settings = context.bot_data["settings"]

    # Get services
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")

    logger.info(
        "Processing document upload",
        user_id=user_id,
        filename=document.file_name,
        file_size=document.file_size,
    )

    with tracer.start_as_current_span("telegram.handle_document") as span:
        span.set_attribute("telegram.user_id", user_id)
        if update.effective_chat:
            span.set_attribute("telegram.chat_id", update.effective_chat.id)
        span.set_attribute("telegram.file_name", document.file_name or "")
        span.set_attribute("telegram.file_size", document.file_size or 0)
        span.set_attribute(
            "telegram.has_caption",
            bool(update.message.caption if update.message else ""),
        )

        progress_msg = None
        claude_progress_msg = None

        try:
            # Validate filename using security validator
            if security_validator:
                valid, error = security_validator.validate_filename(document.file_name)
                if not valid:
                    await update.message.reply_text(
                        f"❌ **File Upload Rejected**\n\n{error}"
                    )

                    # Log security violation
                    if audit_logger:
                        await audit_logger.log_security_violation(
                            user_id=user_id,
                            violation_type="invalid_file_upload",
                            details=(f"Filename: {document.file_name}, Error: {error}"),
                            severity="medium",
                        )
                    return

            # Check file size limits
            max_size = 10 * 1024 * 1024  # 10MB
            if document.file_size > max_size:
                await update.message.reply_text(
                    f"❌ **File Too Large**\n\n"
                    f"Maximum file size: {max_size // 1024 // 1024}MB\n"
                    f"Your file: {document.file_size / 1024 / 1024:.1f}MB"
                )
                return

            # Check rate limit for file processing
            file_cost = _estimate_file_processing_cost(document.file_size)
            if rate_limiter:
                allowed, limit_message = await rate_limiter.check_rate_limit(
                    user_id, file_cost
                )
                if not allowed:
                    await update.message.reply_text(f"⏱️ {limit_message}")
                    return

            # Send processing indicator
            await update.message.chat.send_action("upload_document")

            progress_msg = await update.message.reply_text(
                f"📄 Processing file: `{document.file_name}`...",
                parse_mode="Markdown",
            )

            # Check if enhanced file handler is available
            features = context.bot_data.get("features")
            file_handler = features.get_file_handler() if features else None
            prompt = ""

            if file_handler:
                # Use enhanced file handler
                try:
                    processed_file = await file_handler.handle_document_upload(
                        document,
                        user_id,
                        update.message.caption or "Please review this file:",
                    )
                    prompt = processed_file.prompt

                    # Update progress message with file type info
                    await progress_msg.edit_text(
                        f"📄 Processing {processed_file.type} file: `{document.file_name}`...",
                        parse_mode="Markdown",
                    )

                except Exception as handler_error:
                    logger.warning(
                        "Enhanced file handler failed, falling back to basic handler",
                        error=str(handler_error),
                    )
                    file_handler = None  # Fall back to basic handling

            if not file_handler:
                # Fall back to basic file handling
                file = await document.get_file()
                file_bytes = await file.download_as_bytearray()

                # Try to decode as text
                try:
                    content = file_bytes.decode("utf-8")

                    # Check content length
                    max_content_length = 50000  # 50KB of text
                    if len(content) > max_content_length:
                        content = (
                            content[:max_content_length]
                            + "\n... (file truncated for processing)"
                        )

                    # Create prompt with file content
                    caption = update.message.caption or "Please review this file:"
                    prompt = (
                        f"{caption}\n\n**File:** `{document.file_name}`\n\n```\n"
                        f"{content}\n```"
                    )

                except UnicodeDecodeError:
                    await progress_msg.edit_text(
                        "❌ **File Format Not Supported**\n\n"
                        "File must be text-based and UTF-8 encoded.\n\n"
                        "**Supported formats:**\n"
                        "• Source code files (.py, .js, .ts, etc.)\n"
                        "• Text files (.txt, .md)\n"
                        "• Configuration files (.json, .yaml, .toml)\n"
                        "• Documentation files"
                    )
                    return

            # Delete progress message
            await progress_msg.delete()

            # Create a new progress message for Claude processing
            claude_progress_msg = await update.message.reply_text(
                "🤖 Processing file with Claude...", parse_mode="Markdown"
            )

            # Get Claude integration from context
            claude_integration = context.bot_data.get("claude_integration")

            if not claude_integration:
                await claude_progress_msg.edit_text(
                    "❌ **Claude integration not available**\n\n"
                    "The Claude Code integration is not properly configured.",
                    parse_mode="Markdown",
                )
                return

            # Get current directory and session
            current_dir = context.user_data.get(
                "current_directory", settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

            # Process with Claude
            try:
                claude_response = await claude_integration.run_command(
                    prompt=prompt,
                    working_directory=current_dir,
                    user_id=user_id,
                    session_id=session_id,
                )

                # Update session ID
                context.user_data["claude_session_id"] = claude_response.session_id

                # Check if Claude changed the working directory and update our
                # tracking
                _update_working_directory_from_claude_response(
                    claude_response, context, settings, user_id
                )

                # Format and send response
                from ..utils.formatting import ResponseFormatter

                formatter = ResponseFormatter(settings)
                formatted_messages = formatter.format_claude_response(
                    claude_response.content
                )

                todo_text = _render_todo_list(
                    (context.user_data.get("session_todos") or {}).get(
                        claude_response.session_id, {}
                    )
                )
                if todo_text and formatted_messages:
                    formatted_messages[-1].text = (
                        f"{formatted_messages[-1].text}\n\n{todo_text}"
                    )

                # Delete progress message
                await claude_progress_msg.delete()

                # Send responses
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=message.reply_markup,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )

                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                # Task was cancelled due to new message from same user
                logger.info(
                    "Document processing cancelled due to new message",
                    user_id=user_id,
                )
                try:
                    await claude_progress_msg.delete()
                except Exception:
                    pass
                return
            except Exception as claude_error:
                await claude_progress_msg.edit_text(
                    _format_error_message(str(claude_error)),
                    parse_mode="Markdown",
                )
                logger.exception(
                    "Claude file processing failed",
                    error=str(claude_error),
                    user_id=user_id,
                )

            # Log successful file processing
            if audit_logger:
                await audit_logger.log_file_access(
                    user_id=user_id,
                    file_path=document.file_name,
                    action="upload_processed",
                    success=True,
                    file_size=document.file_size,
                )

        except Exception as e:
            # Attempt to clean up progress messages
            try:
                if progress_msg is not None:
                    await progress_msg.delete()
                if claude_progress_msg is not None:
                    await claude_progress_msg.delete()
            except Exception:
                logger.warning(
                    "Failed to delete progress messages during error handling",
                    user_id=user_id,
                )

            if span.is_recording():
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))

            error_msg = f"❌ **Error processing file**\n\n{str(e)}"
            await update.message.reply_text(error_msg, parse_mode="Markdown")

            # Log failed file processing
            if audit_logger:
                await audit_logger.log_file_access(
                    user_id=user_id,
                    file_path=document.file_name,
                    action="upload_failed",
                    success=False,
                    file_size=document.file_size,
                )

            logger.exception("Error processing document", error=str(e), user_id=user_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Check if enhanced image handler is available
    features = context.bot_data.get("features")
    image_handler = features.get_image_handler() if features else None

    if image_handler:
        with tracer.start_as_current_span("telegram.handle_photo") as span:
            span.set_attribute("telegram.user_id", user_id)
            if update.effective_chat:
                span.set_attribute("telegram.chat_id", update.effective_chat.id)
            has_caption = bool(update.message.caption if update.message else "")
            span.set_attribute("telegram.has_caption", has_caption)

            progress_msg = None
            claude_progress_msg = None

            try:
                # Send processing indicator
                progress_msg = await update.message.reply_text(
                    "📸 Processing image...", parse_mode="Markdown"
                )

                # Get the largest photo size
                photo = update.message.photo[-1]

                # Process image with enhanced handler
                processed_image = await image_handler.process_image(
                    photo, update.message.caption
                )

                # Delete progress message
                await progress_msg.delete()

                # Create Claude progress message
                claude_progress_msg = await update.message.reply_text(
                    "🤖 Analyzing image with Claude...", parse_mode="Markdown"
                )

                # Get Claude integration
                claude_integration = context.bot_data.get("claude_integration")

                if not claude_integration:
                    await claude_progress_msg.edit_text(
                        "❌ **Claude integration not available**\n\n"
                        "The Claude Code integration is not properly configured.",
                        parse_mode="Markdown",
                    )
                    return

                # Get current directory and session
                current_dir = context.user_data.get(
                    "current_directory", settings.approved_directory
                )
                session_id = context.user_data.get("claude_session_id")

                # Process with Claude
                try:
                    claude_response = await claude_integration.run_command(
                        prompt=processed_image.prompt,
                        working_directory=current_dir,
                        user_id=user_id,
                        session_id=session_id,
                    )

                    # Update session ID
                    context.user_data["claude_session_id"] = claude_response.session_id

                    # Format and send response
                    from ..utils.formatting import ResponseFormatter

                    formatter = ResponseFormatter(settings)
                    formatted_messages = formatter.format_claude_response(
                        claude_response.content
                    )

                    todo_text = _render_todo_list(
                        (context.user_data.get("session_todos") or {}).get(
                            claude_response.session_id, {}
                        )
                    )
                    if todo_text and formatted_messages:
                        formatted_messages[-1].text = (
                            f"{formatted_messages[-1].text}\n\n{todo_text}"
                        )

                    # Delete progress message
                    await claude_progress_msg.delete()

                    # Send responses
                    for i, message in enumerate(formatted_messages):
                        await update.message.reply_text(
                            message.text,
                            parse_mode=message.parse_mode,
                            reply_markup=message.reply_markup,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

                        if i < len(formatted_messages) - 1:
                            await asyncio.sleep(0.5)

                except asyncio.CancelledError:
                    # Task was cancelled due to new message from same user
                    logger.info(
                        "Photo processing cancelled due to new message",
                        user_id=user_id,
                    )
                    try:
                        await claude_progress_msg.delete()
                    except Exception:
                        pass
                    return
                except Exception as claude_error:
                    await claude_progress_msg.edit_text(
                        _format_error_message(str(claude_error)),
                        parse_mode="Markdown",
                    )
                    logger.exception(
                        "Claude image processing failed",
                        error=str(claude_error),
                        user_id=user_id,
                    )

            except Exception as e:
                try:
                    if progress_msg is not None:
                        await progress_msg.delete()
                    if claude_progress_msg is not None:
                        await claude_progress_msg.delete()
                except Exception:
                    logger.warning(
                        "Failed to delete image progress messages during error handling",
                        user_id=user_id,
                    )

                if span.is_recording():
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))

                await update.message.reply_text(
                    f"❌ **Error processing image**\n\n{str(e)}", parse_mode="Markdown"
                )

    else:
        # Fall back to unsupported message
        await update.message.reply_text(
            "📸 **Photo Upload**\n\n"
            "Photo processing is not yet supported.\n\n"
            "**Currently supported:**\n"
            "• Text files (.py, .js, .md, etc.)\n"
            "• Configuration files\n"
            "• Documentation files\n\n"
            "**Coming soon:**\n"
            "• Image analysis\n"
            "• Screenshot processing\n"
            "• Diagram interpretation"
        )


def _estimate_text_processing_cost(text: str) -> float:
    """Estimate cost for processing text message."""
    # Base cost
    base_cost = 0.001

    # Additional cost based on length
    length_cost = len(text) * 0.00001

    # Additional cost for complex requests
    complex_keywords = [
        "analyze",
        "generate",
        "create",
        "build",
        "implement",
        "refactor",
        "optimize",
        "debug",
        "explain",
        "document",
    ]

    text_lower = text.lower()
    complexity_multiplier = 1.0

    for keyword in complex_keywords:
        if keyword in text_lower:
            complexity_multiplier += 0.5

    return (base_cost + length_cost) * min(complexity_multiplier, 3.0)


def _estimate_file_processing_cost(file_size: int) -> float:
    """Estimate cost for processing uploaded file."""
    # Base cost for file handling
    base_cost = 0.005

    # Additional cost based on file size (per KB)
    size_cost = (file_size / 1024) * 0.0001

    return base_cost + size_cost


async def _generate_placeholder_response(
    message_text: str, context: ContextTypes.DEFAULT_TYPE
) -> dict:
    """Generate placeholder response until Claude integration is implemented."""
    settings: Settings = context.bot_data["settings"]
    current_dir = getattr(
        context.user_data, "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Analyze the message for intent
    message_lower = message_text.lower()

    if any(
        word in message_lower for word in ["list", "show", "see", "directory", "files"]
    ):
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I understand you want to see files. Try using the `/ls` command to list files "
            f"in your current directory (`{relative_path}/`).\n\n"
            f"**Available commands:**\n"
            f"• `/ls` - List files\n"
            f"• `/cd <dir>` - Change directory\n"
            f"• `/projects` - Show projects\n\n"
            f"_Note: Full Claude Code integration will be available in the next phase._"
        )

    elif any(word in message_lower for word in ["create", "generate", "make", "build"]):
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I understand you want to create something! Once the Claude Code integration "
            f"is complete, I'll be able to:\n\n"
            f"• Generate code files\n"
            f"• Create project structures\n"
            f"• Write documentation\n"
            f"• Build complete applications\n\n"
            f"**Current directory:** `{relative_path}/`\n\n"
            f"_Full functionality coming soon!_"
        )

    elif any(word in message_lower for word in ["help", "how", "what", "explain"]):
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I'm here to help! Try using `/help` for available commands.\n\n"
            f"**What I can do now:**\n"
            f"• Navigate directories (`/cd`, `/ls`, `/pwd`)\n"
            f"• Show projects (`/projects`)\n"
            f"• Manage sessions (`/new`, `/status`)\n\n"
            f"**Coming soon:**\n"
            f"• Full Claude Code integration\n"
            f"• Code generation and editing\n"
            f"• File operations\n"
            f"• Advanced programming assistance"
        )

    else:
        response_text = (
            f"🤖 **Claude Code Response** _(Placeholder)_\n\n"
            f"I received your message: \"{message_text[:100]}{'...' if len(message_text) > 100 else ''}\"\n\n"
            f"**Current Status:**\n"
            f"• Directory: `{relative_path}/`\n"
            f"• Bot core: ✅ Active\n"
            f"• Claude integration: 🔄 Coming soon\n\n"
            f"Once Claude Code integration is complete, I'll be able to process your "
            f"requests fully and help with coding tasks!\n\n"
            f"For now, try the available commands like `/ls`, `/cd`, and `/help`."
        )

    return {"text": response_text, "parse_mode": "Markdown"}


def _update_working_directory_from_claude_response(
    claude_response, context, settings, user_id
):
    """Update the working directory based on Claude's response content."""
    import re
    from pathlib import Path

    # Look for directory changes in Claude's response
    # This searches for common patterns that indicate directory changes
    patterns = [
        r"(?:^|\n).*?cd\s+([^\s\n]+)",  # cd command
        r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",  # explicit directory change
        r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",  # current directory indication
        r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",  # working directory indication
    ]

    content = claude_response.content.lower()
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                # Clean up the path
                new_path = match.strip().strip("\"'`")

                # Handle relative paths
                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    # Relative path without ./
                    new_path = (current_dir / new_path).resolve()
                else:
                    # Absolute path
                    new_path = Path(new_path).resolve()

                # Validate that the new path is within the approved directory
                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.exists()
                ):
                    context.user_data["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Claude response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return  # Take the first valid match

            except (ValueError, OSError) as e:
                # Invalid path, skip this match
                logger.debug(
                    "Invalid path in Claude response", path=match, error=str(e)
                )
                continue
