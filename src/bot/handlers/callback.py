"""Handle inline keyboard callbacks."""

import asyncio
import re

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from .message import (
    _format_tool_name,
    _format_tool_params,
    _normalize_todo_payload,
    _render_todo_list,
)

logger = structlog.get_logger()


def _markdown_to_html(text: str) -> str:
    """Convert basic Markdown to Telegram HTML.

    Handles:
    - **bold** -> <b>bold</b>
    - *italic* or _italic_ -> <i>italic</i>
    - `code` -> <code>code</code>
    - ```code block``` -> <pre>code block</pre>
    - [text](url) -> <a href="url">text</a>
    - Headers (# ## ###) -> <b>Header</b>
    """
    # Escape HTML special chars first (but preserve our conversions)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # Code blocks (must be before inline code)
    text = re.sub(
        r"```(?:\w+)?\n?(.*?)```",
        r"<pre>\1</pre>",
        text,
        flags=re.DOTALL,
    )

    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # Bold (**text** or __text__)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # Italic (*text* or _text_) - careful not to match inside words
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)

    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Headers (# Header) -> bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    return text


async def _safe_send_message(
    message,
    text: str,
    parse_mode: str = "Markdown",
    **kwargs,
):
    """Send message with fallback if parse mode fails.

    Tries: Markdown -> HTML -> Plain text
    """
    try:
        return await message.reply_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if "can't parse entities" in str(e).lower():
            logger.warning(
                "Markdown parse failed, trying HTML",
                error=str(e),
            )
            try:
                # Convert to HTML
                html_text = _markdown_to_html(text)
                return await message.reply_text(html_text, parse_mode="HTML", **kwargs)
            except BadRequest as e2:
                logger.warning(
                    "HTML parse failed, sending plain text",
                    error=str(e2),
                )
                # Strip all formatting and send plain
                plain_text = re.sub(r"[*_`#\[\]()]", "", text)
                return await message.reply_text(plain_text, **kwargs)
        raise


def _split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """Split long text into Telegram-safe chunks."""
    parts: list[str] = []
    remaining = text

    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < int(limit * 0.5):
            split_idx = limit
        parts.append(remaining[:split_idx].rstrip())
        remaining = remaining[split_idx:]

    if remaining.strip():
        parts.append(remaining.strip())

    return parts


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback

    user_id = query.from_user.id
    data = query.data

    logger.info("Processing callback query", user_id=user_id, callback_data=data)

    try:
        # Parse callback data
        if ":" in data:
            action, param = data.split(":", 1)
        else:
            action, param = data, None

        # Route to appropriate handler
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "quick_action": handle_quick_action_callback,  # Support both formats
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
            "pcmd": handle_project_command_callback,  # Project commands from .claude/commands
        }

        handler = handlers.get(action)
        if handler:
            await handler(query, param, context)
        else:
            await query.edit_message_text(
                "❌ **Unknown Action**\n\n"
                "This button action is not recognized. "
                "The bot may have been updated since this message was sent."
            )

    except Exception as e:
        logger.exception(
            "Error handling callback query",
            error=str(e),
            user_id=user_id,
            callback_data=data,
        )

        try:
            await query.edit_message_text(
                "❌ **Error Processing Action**\n\n"
                "An error occurred while processing your request.\n"
                "Please try again or use text commands."
            )
        except Exception:
            # If we can't edit the message, send a new one
            await query.message.reply_text(
                "❌ **Error Processing Action**\n\n"
                "An error occurred while processing your request."
            )


async def handle_cd_callback(
    query, project_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory change from inline keyboard."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    try:
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )

        # Handle special paths
        if project_name == "/":
            new_path = settings.approved_directory
        elif project_name == "..":
            new_path = current_dir.parent
            # Ensure we don't go above approved directory
            if not str(new_path).startswith(str(settings.approved_directory)):
                new_path = settings.approved_directory
        else:
            new_path = settings.approved_directory / project_name

        # Validate path if security validator is available
        if security_validator:
            # Pass the absolute path for validation
            valid, resolved_path, error = security_validator.validate_path(
                str(new_path), settings.approved_directory
            )
            if not valid:
                await query.edit_message_text(f"❌ **Access Denied**\n\n{error}")
                return
            # Use the validated path
            new_path = resolved_path

        # Check if directory exists
        if not new_path.exists() or not new_path.is_dir():
            await query.edit_message_text(
                f"❌ **Directory Not Found**\n\n"
                f"The directory `{project_name}` no longer exists or is not accessible."
            )
            return

        # Update directory and clear session
        context.user_data["current_directory"] = new_path
        context.user_data["claude_session_id"] = None

        # Send confirmation with new directory info
        relative_path = new_path.relative_to(settings.approved_directory)

        # Add navigation buttons
        keyboard = [
            [
                InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ **Directory Changed**\n\n"
            f"📂 Current directory: `{relative_path}/`\n\n"
            f"🔄 Claude session cleared. You can now start coding in this directory!",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

        # Log successful directory change
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=True
            )

    except Exception as e:
        await query.edit_message_text(f"❌ **Error changing directory**\n\n{str(e)}")

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=False
            )


async def handle_action_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle general action callbacks."""
    actions = {
        "help": _handle_help_action,
        "show_projects": _handle_show_projects_action,
        "new_session": _handle_new_session_action,
        "continue": _handle_continue_action,
        "end_session": _handle_end_session_action,
        "status": _handle_status_action,
        "ls": _handle_ls_action,
        "start_coding": _handle_start_coding_action,
        "quick_actions": _handle_quick_actions_action,
        "refresh_status": _handle_refresh_status_action,
        "refresh_ls": _handle_refresh_ls_action,
        "export": _handle_export_action,
    }

    handler = actions.get(action_type)
    if handler:
        await handler(query, context)
    else:
        await query.edit_message_text(
            f"❌ **Unknown Action: {action_type}**\n\n"
            "This action is not implemented yet."
        )


async def handle_confirm_callback(
    query, confirmation_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle confirmation dialogs."""
    if confirmation_type == "yes":
        await query.edit_message_text("✅ **Confirmed**\n\nAction will be processed.")
    elif confirmation_type == "no":
        await query.edit_message_text("❌ **Cancelled**\n\nAction was cancelled.")
    else:
        await query.edit_message_text("❓ **Unknown confirmation response**")


# Action handlers


async def _handle_help_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help action."""
    help_text = (
        "🤖 **Quick Help**\n\n"
        "**Navigation:**\n"
        "• `/ls` - List files\n"
        "• `/cd <dir>` - Change directory\n"
        "• `/projects` - Show projects\n\n"
        "**Sessions:**\n"
        "• `/new` - New Claude session\n"
        "• `/status` - Session status\n\n"
        "**Tips:**\n"
        "• Send any text to interact with Claude\n"
        "• Upload files for code review\n"
        "• Use buttons for quick actions\n\n"
        "Use `/help` for detailed help."
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 Full Help", callback_data="action:full_help"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="action:main_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        help_text, parse_mode="Markdown", reply_markup=reply_markup
    )


async def _handle_show_projects_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle show projects action."""
    settings: Settings = context.bot_data["settings"]

    try:
        # Get directories in approved directory
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await query.edit_message_text(
                "📁 **No Projects Found**\n\n"
                "No subdirectories found in your approved directory.\n"
                "Create some directories to organize your projects!"
            )
            return

        # Create project buttons
        keyboard = []
        for i in range(0, len(projects), 2):
            row = []
            for j in range(2):
                if i + j < len(projects):
                    project = projects[i + j]
                    row.append(
                        InlineKeyboardButton(
                            f"📁 {project}", callback_data=f"cd:{project}"
                        )
                    )
            keyboard.append(row)

        # Add navigation buttons
        keyboard.append(
            [
                InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)
        project_list = "\n".join([f"• `{project}/`" for project in projects])

        await query.edit_message_text(
            f"📁 **Available Projects**\n\n"
            f"{project_list}\n\n"
            f"Click a project to navigate to it:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Error loading projects: {str(e)}")
        logger.exception("Error in _handle_show_projects_action", error=str(e))


async def _handle_new_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new session action."""
    settings: Settings = context.bot_data["settings"]

    # Clear session
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Start Coding", callback_data="action:start_coding"
            ),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Quick Actions", callback_data="action:quick_actions"
            ),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🆕 **New Claude Code Session**\n\n"
        f"📂 Working directory: `{relative_path}/`\n\n"
        f"Ready to help you code! Send me a message to get started:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def _handle_end_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle end session action."""
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await query.edit_message_text(
            "ℹ️ **No Active Session**\n\n"
            "There's no active Claude session to end.\n\n"
            "**What you can do:**\n"
            "• Use the button below to start a new session\n"
            "• Check your session status\n"
            "• Send any message to start a conversation",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 New Session", callback_data="action:new_session"
                        )
                    ],
                    [InlineKeyboardButton("📊 Status", callback_data="action:status")],
                ]
            ),
        )
        return

    # Get current directory for display
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Clear session data
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = False
    context.user_data["last_message"] = None

    # Create quick action buttons
    keyboard = [
        [
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="action:status"),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "✅ **Session Ended**\n\n"
        f"Your Claude session has been terminated.\n\n"
        f"**Current Status:**\n"
        f"• Directory: `{relative_path}/`\n"
        f"• Session: None\n"
        f"• Ready for new commands\n\n"
        f"**Next Steps:**\n"
        f"• Start a new session\n"
        f"• Check status\n"
        f"• Send any message to begin a new conversation",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def _handle_continue_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle continue session action."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await query.edit_message_text(
                "❌ **Claude Integration Not Available**\n\n"
                "Claude integration is not properly configured."
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")

        if claude_session_id:
            # Continue with the existing session (no prompt = use --continue)
            await query.edit_message_text(
                f"🔄 **Continuing Session**\n\n"
                f"Session ID: `{claude_session_id[:8]}...`\n"
                f"Directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
                f"Continuing where you left off...",
                parse_mode="Markdown",
            )

            claude_response = await claude_integration.run_command(
                prompt="",  # Empty prompt triggers --continue
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            # No session in context, try to find the most recent session
            await query.edit_message_text(
                "🔍 **Looking for Recent Session**\n\n"
                "Searching for your most recent session in this directory...",
                parse_mode="Markdown",
            )

            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=None,  # No prompt = use --continue
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Send Claude's response
            await query.message.reply_text(
                f"✅ **Session Continued**\n\n"
                f"{claude_response.content[:500]}{'...' if len(claude_response.content) > 500 else ''}",
                parse_mode="Markdown",
            )
        else:
            # No session found to continue
            await query.edit_message_text(
                "❌ **No Session Found**\n\n"
                f"No recent Claude session found in this directory.\n"
                f"Directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
                f"**What you can do:**\n"
                f"• Use the button below to start a fresh session\n"
                f"• Check your session status\n"
                f"• Navigate to a different directory",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 New Session", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 Status", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        logger.exception("Error in continue action", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ **Error Continuing Session**\n\n"
            f"An error occurred: `{str(e)}`\n\n"
            f"Try starting a new session instead.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 New Session", callback_data="action:new_session"
                        )
                    ]
                ]
            ),
        )


async def _handle_status_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle status action."""
    from telegram.error import BadRequest

    # This essentially duplicates the /status command functionality
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get usage info if rate limiter is available
    rate_limiter = context.bot_data.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f"💰 Usage: ${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = "💰 Usage: _Unable to retrieve_\n"

    status_lines = [
        "📊 **Session Status**",
        "",
        f"📂 Directory: `{relative_path}/`",
        f"🤖 Claude Session: {'✅ Active' if claude_session_id else '❌ None'}",
        usage_info.rstrip(),
    ]

    if claude_session_id:
        status_lines.append(f"🆔 Session ID: `{claude_session_id[:8]}...`")

    # Add action buttons
    keyboard = []
    if claude_session_id:
        keyboard.append(
            [
                InlineKeyboardButton("🔄 Continue", callback_data="action:continue"),
                InlineKeyboardButton(
                    "🛑 End Session", callback_data="action:end_session"
                ),
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 Start Session", callback_data="action:new_session"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_status"),
            InlineKeyboardButton("📁 Projects", callback_data="action:show_projects"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            "\n".join(status_lines), parse_mode="Markdown", reply_markup=reply_markup
        )
    except BadRequest as e:
        # If message is not modified, just answer the callback
        if "message is not modified" in str(e).lower():
            await query.answer("✅ Status is up to date")
        else:
            raise


async def _handle_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ls action."""
    from telegram.error import BadRequest

    settings: Settings = context.bot_data["settings"]
    user_id = query.from_user.id
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents (similar to /ls command)
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            if item.name.startswith("."):
                continue

            if item.is_dir():
                directories.append(f"📁 {_escape_markdown(item.name)}/")
            else:
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {_escape_markdown(item.name)} ({size_str})")
                except OSError:
                    files.append(f"📄 {_escape_markdown(item.name)}")

        items = directories + files
        relative_path = current_dir.relative_to(settings.approved_directory)
        escaped_path = _escape_markdown(str(relative_path))

        if not items:
            message = f"📂 `{escaped_path}/`\n\n_(empty directory)_"
        else:
            message = f"📂 `{escaped_path}/`\n\n"
            max_items = 30  # Limit for inline display
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n_... and {len(items) - max_items} more items_"
            else:
                message += "\n".join(items)

        # Add buttons
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_text(
                message, parse_mode="Markdown", reply_markup=reply_markup
            )
        except BadRequest as e:
            # If message is not modified, just answer the callback
            if "message is not modified" in str(e).lower():
                await query.answer("✅ Directory is up to date")
            else:
                raise

    except Exception as e:
        error_message = f"❌ Error listing directory: {str(e)}"
        logger.exception(
            "Error in ls action",
            error=str(e),
            user_id=user_id,
        )
        try:
            await query.edit_message_text(error_message)
        except BadRequest:
            # If we can't edit, send as a popup
            await query.answer(error_message, show_alert=True)


async def _handle_start_coding_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle start coding action."""
    await query.edit_message_text(
        "🚀 **Ready to Code!**\n\n"
        "Send me any message to start coding with Claude:\n\n"
        "**Examples:**\n"
        '• _"Create a Python script that..."_\n'
        '• _"Help me debug this code..."_\n'
        '• _"Explain how this file works..."_\n'
        "• Upload a file for review\n\n"
        "I'm here to help with all your coding needs!"
    )


async def _handle_quick_actions_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick actions menu."""
    settings: Settings = context.bot_data["settings"]

    # Get quick actions from features
    features = context.bot_data.get("features")
    if not features or not features.is_enabled("quick_actions"):
        await query.edit_message_text(
            "❌ **Quick Actions Disabled**\n\n" "Quick actions feature is not enabled."
        )
        return

    quick_action_manager = features.get_quick_actions()
    if not quick_action_manager:
        await query.edit_message_text(
            "❌ **Quick Actions Unavailable**\n\n"
            "Quick actions service is not available."
        )
        return

    # Get context-aware actions
    actions = await quick_action_manager.get_suggestions(session=None)

    if not actions:
        await query.edit_message_text(
            "🤖 **No Actions Available**\n\n"
            "No quick actions are available for the current context.\n\n"
            "**Try:**\n"
            "• Navigating to a project directory\n"
            "• Creating some code files\n"
            "• Starting a Claude session"
        )
        return

    # Create inline keyboard from actions
    keyboard_buttons = quick_action_manager.create_inline_keyboard(actions, columns=2)

    # Add back button
    keyboard = keyboard_buttons.inline_keyboard + [
        [InlineKeyboardButton("⬅️ Back", callback_data="action:new_session")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    await query.edit_message_text(
        f"⚡ **Quick Actions**\n\n"
        f"📂 Context: `{relative_path}/`\n\n"
        f"Select an action to execute:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def _handle_refresh_status_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle refresh status action."""
    await _handle_status_action(query, context)


async def _handle_refresh_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle refresh ls action."""
    await _handle_ls_action(query, context)


async def _handle_export_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle export action."""
    await query.edit_message_text(
        "📤 **Export Session**\n\n"
        "Session export functionality will be available once the storage layer is implemented.\n\n"
        "**Planned features:**\n"
        "• Export conversation history\n"
        "• Save session state\n"
        "• Share conversations\n"
        "• Create session backups\n\n"
        "_Coming in the next development phase!_"
    )


async def handle_quick_action_callback(
    query, action_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick action callbacks."""
    user_id = query.from_user.id

    # Get quick actions manager from features
    features = context.bot_data.get("features")
    quick_actions = features.get_quick_actions() if features else None

    if not quick_actions:
        await query.edit_message_text(
            "❌ **Quick Actions Not Available**\n\n"
            "Quick actions feature is not available."
        )
        return

    # Get Claude integration
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await query.edit_message_text(
            "❌ **Claude Integration Not Available**\n\n"
            "Claude integration is not properly configured."
        )
        return

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # Check if this is a special action that should be redirected
        if action_id == "git_status":
            # Redirect to git handler
            await handle_git_callback(query, "status", context)
            return
        elif action_id in ["find_todos", "build", "start"]:
            # These are legacy actions that aren't implemented yet
            await query.edit_message_text(
                f"🚧 **Action Not Implemented**\n\n"
                f"The '{action_id}' action is not yet implemented.\n\n"
                f"You can ask Claude to help with this task by sending a message."
            )
            return

        # Get the action from the manager
        action = quick_actions.actions.get(action_id)
        if not action:
            await query.edit_message_text(
                f"❌ **Action Not Found**\n\n"
                f"Quick action '{action_id}' is not available."
            )
            return

        # Execute the action
        await query.edit_message_text(
            f"🚀 **Executing {action.icon} {action.name}**\n\n"
            f"Running quick action in directory: `{current_dir.relative_to(settings.approved_directory)}/`\n\n"
            f"Please wait...",
            parse_mode="Markdown",
        )

        # Run the action through Claude
        claude_response = await claude_integration.run_command(
            prompt=action.command, working_directory=current_dir, user_id=user_id
        )

        if claude_response:
            # Format and send the response
            response_text = claude_response.content
            if len(response_text) > 4000:
                response_text = response_text[:4000] + "...\n\n_(Response truncated)_"

            await query.message.reply_text(
                f"✅ **{action.icon} {action.name} Complete**\n\n{response_text}",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ **Action Failed**\n\n"
                f"Failed to execute {action.name}. Please try again."
            )

    except Exception as e:
        logger.exception("Quick action execution failed", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ **Action Error**\n\n"
            f"An error occurred while executing {action_id}: {str(e)}"
        )


async def handle_followup_callback(
    query, suggestion_hash: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle follow-up suggestion callbacks."""
    user_id = query.from_user.id

    # Get conversation enhancer from bot data if available
    conversation_enhancer = context.bot_data.get("conversation_enhancer")

    if not conversation_enhancer:
        await query.edit_message_text(
            "❌ **Follow-up Not Available**\n\n"
            "Conversation enhancement features are not available."
        )
        return

    try:
        # Get stored suggestions (this would need to be implemented in the enhancer)
        # For now, we'll provide a generic response
        await query.edit_message_text(
            "💡 **Follow-up Suggestion Selected**\n\n"
            "This follow-up suggestion will be implemented once the conversation "
            "enhancement system is fully integrated with the message handler.\n\n"
            "**Current Status:**\n"
            "• Suggestion received ✅\n"
            "• Integration pending 🔄\n\n"
            "_You can continue the conversation by sending a new message._"
        )

        logger.info(
            "Follow-up suggestion selected",
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

    except Exception as e:
        logger.exception(
            "Error handling follow-up callback",
            error=str(e),
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

        await query.edit_message_text(
            "❌ **Error Processing Follow-up**\n\n"
            "An error occurred while processing your follow-up suggestion."
        )


async def handle_conversation_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle conversation control callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    if action_type == "continue":
        # Remove suggestion buttons and show continue message
        await query.edit_message_text(
            "✅ **Continuing Conversation**\n\n"
            "Send me your next message to continue coding!\n\n"
            "I'm ready to help with:\n"
            "• Code review and debugging\n"
            "• Feature implementation\n"
            "• Architecture decisions\n"
            "• Testing and optimization\n"
            "• Documentation\n\n"
            "_Just type your request or upload files._"
        )

    elif action_type == "end":
        # End the current session
        conversation_enhancer = context.bot_data.get("conversation_enhancer")
        if conversation_enhancer:
            conversation_enhancer.clear_context(user_id)

        # Clear session data
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = False

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        relative_path = current_dir.relative_to(settings.approved_directory)

        # Create quick action buttons
        keyboard = [
            [
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
                InlineKeyboardButton(
                    "📁 Change Project", callback_data="action:show_projects"
                ),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
                InlineKeyboardButton("❓ Help", callback_data="action:help"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "✅ **Conversation Ended**\n\n"
            f"Your Claude session has been terminated.\n\n"
            f"**Current Status:**\n"
            f"• Directory: `{relative_path}/`\n"
            f"• Session: None\n"
            f"• Ready for new commands\n\n"
            f"**Next Steps:**\n"
            f"• Start a new session\n"
            f"• Check status\n"
            f"• Send any message to begin a new conversation",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

        logger.info("Conversation ended via callback", user_id=user_id)

    else:
        await query.edit_message_text(
            f"❌ **Unknown Conversation Action: {action_type}**\n\n"
            "This conversation action is not recognized."
        )


async def handle_git_callback(
    query, git_action: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle git-related callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await query.edit_message_text(
            "❌ **Git Integration Disabled**\n\n"
            "Git integration feature is not enabled."
        )
        return

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await query.edit_message_text(
                "❌ **Git Integration Unavailable**\n\n"
                "Git integration service is not available."
            )
            return

        if git_action == "status":
            # Refresh git status
            git_status = await git_integration.get_status(current_dir)
            status_message = git_integration.format_status(git_status)

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="git:status"),
                    InlineKeyboardButton("📁 Files", callback_data="action:ls"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                status_message, parse_mode="Markdown", reply_markup=reply_markup
            )

        elif git_action == "diff":
            # Show git diff
            diff_output = await git_integration.get_diff(current_dir)

            if not diff_output.strip():
                diff_message = "📊 **Git Diff**\n\n_No changes to show._"
            else:
                # Clean up diff output for Telegram
                # Remove emoji symbols that interfere with markdown parsing
                clean_diff = (
                    diff_output.replace("➕", "+").replace("➖", "-").replace("📍", "@")
                )

                # Limit diff output
                max_length = 2000
                if len(clean_diff) > max_length:
                    clean_diff = (
                        clean_diff[:max_length] + "\n\n_... output truncated ..._"
                    )

                diff_message = f"📊 **Git Diff**\n\n```\n{clean_diff}\n```"

            keyboard = [
                [
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                diff_message, parse_mode="Markdown", reply_markup=reply_markup
            )

        elif git_action == "log":
            # Show git log
            commits = await git_integration.get_file_history(current_dir, ".")

            if not commits:
                log_message = "📜 **Git Log**\n\n_No commits found._"
            else:
                log_message = "📜 **Git Log**\n\n"
                for commit in commits[:10]:  # Show last 10 commits
                    short_hash = commit.hash[:7]
                    short_message = commit.message[:60]
                    if len(commit.message) > 60:
                        short_message += "..."
                    log_message += f"• `{short_hash}` {short_message}\n"

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                log_message, parse_mode="Markdown", reply_markup=reply_markup
            )

        else:
            await query.edit_message_text(
                f"❌ **Unknown Git Action: {git_action}**\n\n"
                "This git action is not recognized."
            )

    except Exception as e:
        logger.exception(
            "Error in git callback",
            error=str(e),
            git_action=git_action,
            user_id=user_id,
        )
        await query.edit_message_text(f"❌ **Git Error**\n\n{str(e)}")


async def handle_export_callback(
    query, export_format: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle export format selection callbacks."""
    user_id = query.from_user.id
    features = context.bot_data.get("features")

    if export_format == "cancel":
        await query.edit_message_text(
            "📤 **Export Cancelled**\n\n" "Session export has been cancelled."
        )
        return

    session_exporter = features.get_session_export() if features else None
    if not session_exporter:
        await query.edit_message_text(
            "❌ **Export Unavailable**\n\n" "Session export service is not available."
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")
    if not claude_session_id:
        await query.edit_message_text(
            "❌ **No Active Session**\n\n" "There's no active session to export."
        )
        return

    try:
        # Show processing message
        await query.edit_message_text(
            f"📤 **Exporting Session**\n\n"
            f"Generating {export_format.upper()} export...",
            parse_mode="Markdown",
        )

        # Export session
        exported_session = await session_exporter.export_session(
            claude_session_id, export_format
        )

        # Send the exported file
        from io import BytesIO

        file_bytes = BytesIO(exported_session.content.encode("utf-8"))
        file_bytes.name = exported_session.filename

        await query.message.reply_document(
            document=file_bytes,
            filename=exported_session.filename,
            caption=(
                f"📤 **Session Export Complete**\n\n"
                f"Format: {exported_session.format.upper()}\n"
                f"Size: {exported_session.size_bytes:,} bytes\n"
                f"Created: {exported_session.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode="Markdown",
        )

        # Update the original message
        await query.edit_message_text(
            f"✅ **Export Complete**\n\n"
            f"Your session has been exported as {exported_session.filename}.\n"
            f"Check the file above for your complete conversation history.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception(
            "Export failed", error=str(e), user_id=user_id, format=export_format
        )
        await query.edit_message_text(f"❌ **Export Failed**\n\n{str(e)}")


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"


def _escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown parse mode."""
    escape_chars = r"\_*`["
    for ch in escape_chars:
        text = text.replace(ch, f"\\{ch}")
    return text


async def handle_project_command_callback(
    query, command_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle project command button press from .claude/commands/."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    try:
        # Import project commands module
        from ..features.project_commands import (
            find_command_by_name,
            get_project_commands,
            read_command_content,
        )

        # Get available commands
        commands = get_project_commands(current_dir)
        command = find_command_by_name(commands, command_name)

        if not command:
            await query.edit_message_text(
                f"❌ **Command Not Found**\n\n"
                f"Command `/{command_name}` is not available.\n\n"
                f"The command file may have been removed.\n"
                f"Use `/commands` to see available commands.",
                parse_mode="Markdown",
            )
            return

        def _escape_mdv2(text: str) -> str:
            """Escape MarkdownV2 using telegram helper."""
            return escape_markdown(str(text), version=2)

        # Show executing message (MarkdownV2)
        escaped_command = _escape_mdv2(command_name)
        escaped_dir = _escape_mdv2(str(relative_path))
        start_line = _escape_mdv2("🔄 Starting...")
        progress_msg = await query.edit_message_text(
            f"⏳ Executing /{escaped_command}\n"
            f"📂 {escaped_dir}/\n\n"
            f"{start_line}",
            parse_mode="MarkdownV2",
        )

        # Check if Claude integration is available
        if not claude_integration:
            await query.edit_message_text(
                "❌ **AI Agent Not Available**\n\n"
                "Claude/Cursor integration is not properly configured.\n"
                "Contact your administrator."
            )
            return

        # Read command content (the prompt)
        prompt = read_command_content(command)

        # Stream handler for progress updates
        import time

        last_progress_text = ""
        last_update_time = 0.0
        tool_journal: dict = {}
        tool_order: list[str] = []
        use_journal = claude_integration.get_agent_type() == "cursor-agent"
        todos: dict[str, dict] = {}
        thinking_frames = [".", "..", "..."]
        thinking_index = 0
        thinking_line: Optional[str] = None
        thinking_thoughts: list[str] = [""]

        def _todo_icon(status: str) -> tuple[str, str]:
            """Map todo status to icon and normalized state."""
            st = (status or "").upper()
            if "DONE" in st or "COMPLETE" in st:
                return "✅", "done"
            if "PROGRESS" in st or "DOING" in st:
                return "🔄", "progress"
            return "⬜️", "pending"

        def _update_todos_from_update(update_obj):
            """Update todos honoring merge flag (default True) using shared renderer."""
            todo_list = None
            merge_flag = True
            metadata = update_obj.metadata or {}
            if metadata.get("tool_name") == "updatetodos":
                tool_args = metadata.get("tool_args", {})
                todo_list = tool_args.get("todos")
                if "merge" in tool_args:
                    merge_flag = bool(tool_args.get("merge"))

            if not todo_list and update_obj.tool_calls:
                first_call = update_obj.tool_calls[0]
                input_args = first_call.get("input", {})
                todo_list = input_args.get("todos")
                if "merge" in input_args:
                    merge_flag = bool(input_args.get("merge"))

            if not todo_list:
                return

            normalized = _normalize_todo_payload(todo_list)

            if not merge_flag:
                todos.clear()
                for tid, item in normalized.items():
                    todos[tid] = item
            else:
                # Merge: preserve existing order, update/append in given order
                for tid, item in normalized.items():
                    todos[tid] = item

        def _build_journal_text() -> str:
            """Render a compact tool journal for the progress message."""
            if not tool_journal or not tool_order:
                return ""

            lines = []
            for call_id in tool_order:
                entry = tool_journal.get(call_id)
                if not entry:
                    continue
                tool_name = _format_tool_name(entry.get("name", "tool"))
                params_str = _format_tool_params(entry.get("params", {}))
                icon = entry.get("icon", "⏳")
                status = entry.get("status")
                status_suffix = f" [{status}]" if status else ""
                lines.append(
                    f"{icon} {_escape_mdv2(tool_name)}{_escape_mdv2(params_str)}{_escape_mdv2(status_suffix)}"
                )

            return "\n".join(lines)

        def _build_todo_text() -> str:
            """Render todos with shared renderer (MarkdownV2 escaped)."""
            if not todos:
                return ""
            return (
                _render_todo_list(
                    todos, escape_func=lambda s: escape_markdown(s, version=2)
                )
                or ""
            )

        async def stream_handler(update_obj):
            nonlocal last_progress_text, last_update_time, thinking_index, thinking_line, thinking_thoughts
            try:
                current_time = time.time()

                # Track tool usage in journal when cursor-agent is active
                if use_journal and update_obj.type in {"tool_call", "tool_result"}:
                    metadata = update_obj.metadata or {}
                    call_id = metadata.get("call_id") or metadata.get("tool_use_id")
                    tool_name = metadata.get("tool_name", "tool")

                    params = {}
                    if update_obj.tool_calls and len(update_obj.tool_calls) > 0:
                        tool_call = update_obj.tool_calls[0]
                        tool_name = tool_call.get("name", tool_name)
                        params = tool_call.get("input", {})
                    if not params:
                        params = metadata.get("tool_args", {})

                    if update_obj.type == "tool_call" and call_id:
                        tool_journal[call_id] = {
                            "name": tool_name,
                            "params": params,
                            "status": "running",
                            "icon": "⏳",
                        }
                        if call_id not in tool_order:
                            tool_order.append(call_id)

                    elif update_obj.type == "tool_result" and call_id:
                        is_error = (
                            update_obj.is_error() or metadata.get("status") == "error"
                        )
                        existing = tool_journal.get(call_id, {})
                        if not params:
                            params = existing.get("params", {})
                        tool_journal[call_id] = {
                            "name": existing.get("name", tool_name),
                            "params": params,
                            "status": "error" if is_error else "success",
                            "icon": "❌" if is_error else "✅",
                        }
                        if call_id not in tool_order:
                            tool_order.append(call_id)

                # Build progress text
                _update_todos_from_update(update_obj)

                heading = (
                    f"⏳ Executing /{_escape_mdv2(command_name)}\n"
                    f"📂 {_escape_mdv2(str(relative_path))}/"
                )
                parts = [heading]

                todo_text = _build_todo_text()
                if todo_text:
                    parts.append("")
                    parts.append(todo_text)

                if use_journal:
                    journal_text = _build_journal_text()
                    if journal_text:
                        parts.append("")
                        parts.append(journal_text)

                # Add a short status line for non-journal mode or extra context
                if not use_journal:
                    if update_obj.type == "tool_call":
                        tool_name = (update_obj.metadata or {}).get("tool_name", "tool")
                        parts.append("")
                        parts.append(
                            _escape_mdv2(f"🔧 Running {tool_name.capitalize()}...")
                        )
                    elif update_obj.type == "tool_result":
                        tool_name = (update_obj.metadata or {}).get("tool_name", "tool")
                        parts.append("")
                        parts.append(_escape_mdv2(f"✅ {tool_name.capitalize()} done"))

                # Handle thinking state
                local_thinking = None

                if update_obj.type != "thinking" and thinking_thoughts[-1]:
                    thinking_thoughts.append("")

                # Add assistant or thinking preview
                if update_obj.type == "assistant" and update_obj.content:
                    local_thinking = _escape_mdv2("🤖 Assistant is working")
                elif update_obj.type == "thinking":
                    subtype = (update_obj.metadata or {}).get("subtype")
                    if subtype == "delta":
                        local_thinking = _escape_mdv2("💭 Thinking")
                        thinking_thoughts[-1] += update_obj.content
                    else:
                        content = (update_obj.content or "").strip()
                        if content:
                            local_thinking = f"💭 {_escape_mdv2(content)}"
                        else:
                            local_thinking = _escape_mdv2("💭 Thinking")
                elif update_obj.type == "tool_result":
                    # keep thinking if present but don't add new
                    pass
                elif update_obj.type == "result":
                    thinking_line = None  # clear on final result
                elif update_obj.type == "error":
                    error_msg = update_obj.get_error_message() or "Error"
                    parts.append("")
                    parts.append(f"❌ {_escape_mdv2(error_msg)}")
                elif update_obj.type == "system":
                    local_thinking = _escape_mdv2(
                        f"⚙️ System: {update_obj.metadata["subtype"]}"
                    )

                if local_thinking is not None:
                    thinking_line = local_thinking

                if thinking_line and update_obj.type != "result":
                    dots = thinking_frames[thinking_index % len(thinking_frames)]
                    parts.append("")
                    parts.append(_escape_mdv2(f"{thinking_line}{dots}"))
                    parts.append("")
                    parts.append(_escape_mdv2("\n".join(thinking_thoughts[-2:])))

                progress_text = "\n".join(p for p in parts if p is not None)

                # Throttle updates (0.8 seconds minimum)
                if progress_text and progress_text != last_progress_text:
                    if update_obj.type not in {"thinking", "assistant"} or (
                        current_time - last_update_time >= 0.8
                    ):
                        await progress_msg.edit_text(
                            progress_text, parse_mode="MarkdownV2"
                        )
                        last_progress_text = progress_text
                        thinking_index += 1
                        last_update_time = current_time

            except Exception as e:
                logger.exception("Stream handler error", error=str(e))

        # Execute the command via Claude integration with streaming
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                on_stream=stream_handler,
            )
        except asyncio.CancelledError:
            # Task was cancelled due to new message from same user
            logger.info(
                "Project command cancelled due to new message",
                user_id=user_id,
                command=command_name,
            )
            # Try to update progress message
            try:
                await progress_msg.edit_text(
                    "⏹️ **Command cancelled**\n\n"
                    "Previous command was cancelled due to a new message.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass
            return

        # Update session ID in context if we got one
        if claude_response and claude_response.session_id:
            context.user_data["claude_session_id"] = claude_response.session_id

        if claude_response:
            # Format response
            response_content = claude_response.content or ""
            full_text = f"✅ **/{command_name}** completed\n\n{response_content}"

            # Send full result, split into safe chunks if needed
            for part in _split_for_telegram(full_text):
                await _safe_send_message(
                    query.message,
                    part,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )

            # Log success
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command=f"pcmd:{command_name}",
                    args=[],
                    success=True,
                )

            logger.info(
                "Project command executed",
                user_id=user_id,
                command=command_name,
                duration_ms=claude_response.duration_ms,
            )
        else:
            await query.edit_message_text(
                f"❌ **Command Failed**\n\n"
                f"Failed to execute `/{command_name}`.\n"
                f"Please try again or check the command file.",
                parse_mode="Markdown",
            )

    except Exception as e:
        error_msg = str(e)
        logger.exception(
            "Error executing project command",
            error=error_msg,
            user_id=user_id,
            command=command_name,
        )

        await query.edit_message_text(
            f"❌ **Error Executing Command**\n\n"
            f"Command: `/{command_name}`\n"
            f"Error: `{error_msg[:200]}`\n\n"
            f"Try again or contact support.",
            parse_mode="Markdown",
        )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command=f"pcmd:{command_name}",
                args=[],
                success=False,
            )
