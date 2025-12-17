"""Project commands from .claude/commands directory.

Provides functionality to:
- Scan .claude/commands/ for available commands
- Build inline keyboard with command buttons
- Execute commands from markdown files
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

# Callback data prefix for project commands
PROJECT_COMMAND_PREFIX = "pcmd:"


@dataclass
class ProjectCommand:
    """Represents a project command from .claude/commands/."""

    name: str  # Command name (filename without .md)
    file_path: Path  # Full path to the markdown file
    description: str  # First line from file (title/description)

    def get_callback_data(self) -> str:
        """Get callback data for inline button."""
        return f"{PROJECT_COMMAND_PREFIX}{self.name}"


def get_project_commands(working_directory: Path) -> List[ProjectCommand]:
    """Scan .claude/commands/ directory for available commands.

    Args:
        working_directory: Project root directory

    Returns:
        List of ProjectCommand sorted by name
    """
    commands_dir = working_directory / ".claude" / "commands"

    if not commands_dir.exists():
        logger.debug(
            "No .claude/commands directory found",
            working_directory=str(working_directory),
        )
        return []

    if not commands_dir.is_dir():
        logger.warning(
            ".claude/commands exists but is not a directory",
            path=str(commands_dir),
        )
        return []

    commands = []

    for md_file in commands_dir.glob("*.md"):
        try:
            name = md_file.stem  # filename without .md
            description = _extract_description(md_file)

            commands.append(
                ProjectCommand(
                    name=name,
                    file_path=md_file,
                    description=description,
                )
            )
            logger.debug(
                "Found project command",
                name=name,
                description=description[:50] if description else None,
            )
        except Exception as e:
            logger.warning(
                "Failed to parse command file",
                file=str(md_file),
                error=str(e),
            )
            continue

    # Sort by name for consistent ordering
    return sorted(commands, key=lambda c: c.name)


def _extract_description(file_path: Path) -> str:
    """Extract description from first line of markdown file.

    Typically the first line is a markdown heading like:
    # Command Title

    Returns the title without the # prefix.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()

        # Remove markdown heading prefix
        if first_line.startswith("#"):
            return first_line.lstrip("#").strip()

        return first_line or file_path.stem.replace("-", " ").title()
    except Exception:
        # Fallback to formatted filename
        return file_path.stem.replace("-", " ").title()


def read_command_content(command: ProjectCommand) -> str:
    """Read the full content of a command file.

    Args:
        command: ProjectCommand to read

    Returns:
        Full markdown content of the command file
    """
    return command.file_path.read_text(encoding="utf-8")


def build_commands_keyboard(
    commands: List[ProjectCommand],
    columns: int = 2,
) -> InlineKeyboardMarkup:
    """Build inline keyboard with project commands.

    Args:
        commands: List of ProjectCommand to show as buttons
        columns: Number of buttons per row (default 2)

    Returns:
        InlineKeyboardMarkup with command buttons
    """
    if not commands:
        return InlineKeyboardMarkup([])

    buttons = []
    row = []

    for cmd in commands:
        button = InlineKeyboardButton(
            text=f"/{cmd.name}",
            callback_data=cmd.get_callback_data(),
        )
        row.append(button)

        if len(row) >= columns:
            buttons.append(row)
            row = []

    # Add remaining buttons
    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def format_commands_list(commands: List[ProjectCommand]) -> str:
    """Format commands list for display.

    Args:
        commands: List of ProjectCommand

    Returns:
        Formatted string with command names and descriptions
    """
    if not commands:
        return "No project commands found."

    lines = []
    for cmd in commands:
        lines.append(f"• `/{cmd.name}` — {cmd.description}")

    return "\n".join(lines)


def find_command_by_name(
    commands: List[ProjectCommand],
    name: str,
) -> Optional[ProjectCommand]:
    """Find a command by name.

    Args:
        commands: List of ProjectCommand to search
        name: Command name to find

    Returns:
        ProjectCommand if found, None otherwise
    """
    for cmd in commands:
        if cmd.name == name:
            return cmd
    return None


def parse_callback_data(callback_data: str) -> Optional[str]:
    """Parse command name from callback data.

    Args:
        callback_data: Callback data string like "pcmd:command-name"

    Returns:
        Command name if valid, None otherwise
    """
    if not callback_data.startswith(PROJECT_COMMAND_PREFIX):
        return None

    return callback_data[len(PROJECT_COMMAND_PREFIX) :]


def is_project_command_callback(callback_data: str) -> bool:
    """Check if callback data is for a project command.

    Args:
        callback_data: Callback data string

    Returns:
        True if this is a project command callback
    """
    return callback_data.startswith(PROJECT_COMMAND_PREFIX)
