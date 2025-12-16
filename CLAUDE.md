# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Telegram bot that provides remote access to Claude Code, enabling developers to interact with their code projects through Telegram. The bot integrates with Claude's SDK/CLI to provide AI-powered code assistance, file navigation, and project management features.

**Key Technologies:**
- Python 3.10+ with Poetry for dependency management
- python-telegram-bot for Telegram integration
- Anthropic Claude SDK and Claude CLI
- SQLite with aiosqlite for persistence
- Pydantic for configuration management
- structlog for structured logging
- OpenTelemetry for observability

## Development Commands

### Setup
```bash
# Install dependencies (development mode)
make dev

# Install production dependencies only
make install
```

### Testing
```bash
# Run all tests with coverage
make test

# Run specific test file
.venv/bin/python3 -m pytest tests/unit/test_claude/test_parser.py -v

# Run tests with specific markers
.venv/bin/python3 -m pytest -v -m "not slow"
```

### Code Quality
```bash
# Run all linters (black, isort, flake8, mypy)
make lint

# Auto-format code (black + isort)
make format

# Type checking only
.venv/bin/python3 -m mypy src
```

### Running the Bot
```bash
# Run in production mode
make run

# Run in debug mode with enhanced logging
make run-debug

# Or run directly with Poetry
.venv/bin/python3 -m src.main --debug
```

### Cleanup
```bash
# Clean generated files, caches, build artifacts
make clean
```

## Python Environment

**IMPORTANT:** Always use Python from the virtual environment at `.venv/bin/python3`

This project uses a local virtual environment managed by Poetry. When running commands directly (not through Makefile), use:
```bash
.venv/bin/python3 -m <module>
```

## Architecture

### High-Level Structure

```
src/
├── bot/          # Telegram bot implementation
├── claude/       # Claude Code integration layer
├── config/       # Configuration management
├── security/     # Authentication, authorization, audit
├── storage/      # Database and persistence
├── infra/        # Telemetry and infrastructure
└── utils/        # Shared utilities
```

### Core Components

**Bot Layer** (`src/bot/`)
- `core.py`: Main bot orchestrator with dependency injection
- `handlers/`: Command, message, and callback handlers
- `middleware/`: Security, auth, and rate limiting middleware
- `features/`: Advanced features (git, quick actions, file handling, session export)

**Claude Integration** (`src/claude/`)
- `facade.py`: High-level interface for bot handlers
- `sdk_integration.py`: Python SDK integration (primary)
- `integration.py`: CLI subprocess integration (fallback)
- `session.py`: Session management with persistence
- `monitor.py`: Tool call validation and security monitoring

**Storage** (`src/storage/`)
- `facade.py`: Main storage interface
- `database.py`: SQLite database manager with migrations
- `repositories.py`: Data access layer
- `models.py`: Database models and schemas

**Security** (`src/security/`)
- `auth.py`: Multi-provider authentication (whitelist, token-based)
- `validators.py`: Path traversal, injection prevention
- `rate_limiter.py`: Token bucket algorithm for rate limiting
- `audit.py`: Comprehensive audit logging

**Configuration** (`src/config/`)
- `settings.py`: Pydantic settings with validation
- `loader.py`: Configuration loading from .env
- `features.py`: Feature flags management

### Key Design Patterns

1. **Dependency Injection**: Core dependencies passed through bot context
2. **Repository Pattern**: Storage abstraction in `storage/repositories.py`
3. **Facade Pattern**: Simplified interfaces in `claude/facade.py` and `storage/facade.py`
4. **Middleware Chain**: Security → Auth → Rate Limiting (negative group numbers)
5. **Feature Registry**: Dynamic feature loading in `bot/features/registry.py`

### Claude Integration Modes

The bot supports two Claude integration modes:

1. **SDK Mode** (default, `USE_SDK=true`)
   - Uses `claude-code-sdk` Python package
   - Direct API integration with streaming support
   - Implemented in `claude/sdk_integration.py`
   - Requires CLI authentication OR `ANTHROPIC_API_KEY`

2. **CLI Subprocess Mode** (`USE_SDK=false`)
   - Spawns Claude CLI as subprocess
   - Legacy fallback mechanism
   - Implemented in `claude/integration.py`
   - Requires Claude CLI installed and authenticated

### Session Management

Sessions are persisted to SQLite and include:
- Conversation history with message tracking
- Working directory context
- Usage metrics (tokens, costs)
- Tool call audit trail

Session storage uses UTC timestamps consistently via `utils/datetime_utils.py`.

### Telemetry

OpenTelemetry instrumentation tracks:
- Telegram events (spans for updates, handlers)
- Claude SDK operations (custom instrumentor in `infra/telemetry/claude_sdk_instrumentor.py`)
- Database operations (aiosqlite instrumentor)
- HTTP requests (httpx instrumentation)

Telemetry is optional and controlled by `TELEMETRY_ENABLED` environment variable.

## Configuration

Configuration is loaded from `.env` file using Pydantic Settings.

**Required Settings:**
- `TELEGRAM_BOT_TOKEN`: Bot token from @BotFather
- `TELEGRAM_BOT_USERNAME`: Bot username
- `APPROVED_DIRECTORY`: Base directory for project access
- `ALLOWED_USERS`: Comma-separated Telegram user IDs

**Claude Authentication Options:**
1. Use existing Claude CLI login (recommended): `USE_SDK=true` with no API key
2. Direct API key: `USE_SDK=true` with `ANTHROPIC_API_KEY`
3. CLI subprocess: `USE_SDK=false`

See `.env.example` for complete reference.

## Testing Strategy

Tests are organized in `tests/unit/` mirroring the `src/` structure.

**Key Testing Patterns:**
- `pytest-asyncio` for async tests
- `pytest-mock` for mocking dependencies
- Fixtures in `conftest.py` for common test setup
- High coverage target (>85%)

**Testing Utilities:**
- `tests/conftest.py`: Shared fixtures
- Mock factories for database, Claude responses
- AsyncMock for async dependencies

## Database

SQLite database with migration support:
- Migrations in `src/storage/database.py`
- Schema versioning with `schema_version` table
- Automatic migration on startup
- Models use UTC timestamps via `ensure_utc()` helper

**Main Tables:**
- `sessions`: Claude conversation sessions
- `messages`: Individual messages in sessions
- `users`: User profiles and settings
- `audit_log`: Security and usage audit trail

## Security Considerations

**Multi-Layer Security:**
1. **Path Validation**: All file operations validated against `APPROVED_DIRECTORY`
2. **Authentication**: Whitelist or token-based auth required
3. **Rate Limiting**: Token bucket with configurable limits
4. **Tool Monitoring**: Claude tool calls validated before execution
5. **Audit Logging**: All security events logged

**Disallowed Operations:**
- Directory traversal outside approved directory
- Destructive git operations (`git push`, `git commit` by default)
- Archive bombs and zip bombs (size limits enforced)

## Common Development Tasks

### Adding a New Command

1. Create handler in `src/bot/handlers/command.py`
2. Register in `ClaudeCodeBot._register_handlers()` in `src/bot/core.py`
3. Add to bot commands in `ClaudeCodeBot._set_bot_commands()`
4. Write tests in `tests/unit/test_bot/`

### Adding a New Feature

1. Create feature module in `src/bot/features/`
2. Register in `FeatureRegistry` (`src/bot/features/registry.py`)
3. Add feature flag to `src/config/features.py`
4. Add configuration to `Settings` in `src/config/settings.py`

### Adding Database Migration

1. Update schema in `src/storage/database.py`
2. Increment version number in migration logic
3. Add migration step in `DatabaseManager.migrate()`
4. Update models in `src/storage/models.py`

### Extending Claude Tool Validation

1. Add validation logic to `ToolMonitor` in `src/claude/monitor.py`
2. Update allowed/disallowed tools in `Settings`
3. Add tests for tool validation

## Code Style

- **Formatting**: Black with 88-character line length
- **Import Sorting**: isort with Black profile
- **Type Hints**: Required on all functions (enforced by mypy)
- **Docstrings**: Google-style docstrings for modules and classes
- **Logging**: Use structlog with structured fields

## Observability

The project uses OpenTelemetry for distributed tracing:

- Traces exported to OTLP endpoint (configurable)
- Custom instrumentor for Claude SDK operations
- Structured logging with JSON output option
- Spans include user_id, session_id, working_directory context

Enable with `TELEMETRY_ENABLED=true` in `.env`.

## Dependencies

Dependencies are managed by Poetry in `pyproject.toml`:

**Core Runtime:**
- `python-telegram-bot`: Telegram bot framework
- `anthropic`: Anthropic API client
- `claude-code-sdk`: Claude Code SDK
- `pydantic`: Configuration and validation
- `structlog`: Structured logging
- `aiosqlite`: Async SQLite

**Development:**
- `pytest`, `pytest-asyncio`, `pytest-cov`: Testing
- `black`, `isort`, `flake8`: Formatting and linting
- `mypy`: Static type checking

Update dependencies:
```bash
poetry update
poetry lock
```
