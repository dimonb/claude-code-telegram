# Setup and Installation Guide

This guide provides comprehensive instructions for setting up the Claude Code Telegram Bot with both CLI and SDK integration modes.

## Quick Start

### 1. Prerequisites

- **Python 3.9+** - [Download here](https://www.python.org/downloads/)
- **Poetry** - Modern Python dependency management
- **Telegram Bot Token** - Get one from [@BotFather](https://t.me/botfather)
- **Claude Authentication** - Choose one method below

### 2. Claude Authentication Setup

The bot supports two Claude integration modes. Choose the one that fits your needs:

#### Option A: SDK with CLI Authentication (Recommended)

This method uses the Python SDK for better performance while leveraging your existing Claude CLI authentication.

```bash
# 1. Install Claude CLI
# Visit https://claude.ai/code and follow installation instructions

# 2. Authenticate with Claude
claude auth login

# 3. Verify authentication
claude auth status
# Should show: "✓ You are authenticated"

# 4. Configure bot (in step 4 below)
USE_SDK=true
# Leave ANTHROPIC_API_KEY empty - SDK will use CLI credentials
```

**Pros:**
- Best performance with native async support
- Uses your existing Claude CLI authentication
- Better streaming and error handling
- No need to manage API keys separately

**Cons:**
- Requires Claude CLI installation

#### Option B: SDK with Direct API Key

This method uses the Python SDK with a direct API key, bypassing the need for Claude CLI.

```bash
# 1. Get your API key from https://console.anthropic.com/
# 2. Configure bot (in step 4 below)
USE_SDK=true
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

**Pros:**
- No Claude CLI installation required
- Direct API integration
- Good performance with async support

**Cons:**
- Need to manage API keys manually
- API key management and rotation

#### Option C: CLI Subprocess Mode (Legacy)

This method uses the Claude CLI as a subprocess. Use this only if you need compatibility with older setups.

```bash
# 1. Install Claude CLI
# Visit https://claude.ai/code and follow installation instructions

# 2. Authenticate with Claude
claude auth login

# 3. Configure bot (in step 4 below)
USE_SDK=false
# ANTHROPIC_API_KEY not needed for CLI mode
```

**Pros:**
- Uses official Claude CLI
- Compatible with all CLI features

**Cons:**
- Slower than SDK integration
- Subprocess overhead
- Less reliable error handling

### 3. Install the Bot

```bash
# Clone the repository
git clone https://github.com/yourusername/claude-code-telegram.git
cd claude-code-telegram

# Install Poetry (if needed)
curl -sSL https://install.python-poetry.org | python3 -

# Install dependencies
make dev
```

### 4. Configure Environment

```bash
# Copy the example configuration
cp .env.example .env

# Edit with your settings
nano .env
```

**Required Configuration:**

```bash
# Telegram Bot Settings
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BOT_USERNAME=your_bot_username

# Security
APPROVED_DIRECTORY=/path/to/your/projects
ALLOWED_USERS=123456789  # Your Telegram user ID

# Claude Integration (choose based on your authentication method above)
USE_SDK=true                          # true for SDK, false for CLI
ANTHROPIC_API_KEY=                    # Only needed for Option B above
```

### 5. Get Your Telegram User ID

To configure `ALLOWED_USERS`:

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It will reply with your user ID number
3. Add this number to your `ALLOWED_USERS` setting

### 6. Run the Bot

```bash
# Start in debug mode (recommended for first run)
make run-debug

# Or for production
make run
```

### 7. Test the Bot

1. Find your bot on Telegram (search for your bot username)
2. Send `/start` to begin
3. Try a simple command like `/pwd` or `/ls`
4. Test Claude integration with a simple question

## Advanced Configuration

### Authentication Methods Comparison

| Feature | SDK + CLI Auth | SDK + API Key | CLI Subprocess |
|---------|----------------|---------------|----------------|
| Performance | ✅ Best | ✅ Best | ❌ Slower |
| Setup Complexity | 🟡 Medium | ✅ Easy | 🟡 Medium |
| CLI Required | ✅ Yes | ❌ No | ✅ Yes |
| API Key Management | ❌ No | ✅ Yes | ❌ No |
| Streaming Support | ✅ Yes | ✅ Yes | 🟡 Limited |
| Error Handling | ✅ Best | ✅ Best | 🟡 Basic |

### Security Considerations

#### Directory Isolation
```bash
# Set this to a specific project directory, not your home directory
APPROVED_DIRECTORY=/Users/yourname/projects

# The bot can only access files within this directory
# This prevents access to sensitive system files
```

#### User Access Control
```bash
# Option 1: Whitelist specific users (recommended)
ALLOWED_USERS=123456789,987654321

# Option 2: Token-based authentication
ENABLE_TOKEN_AUTH=true
AUTH_TOKEN_SECRET=your-secret-key-here  # Generate with: openssl rand -hex 32
```

### Rate Limiting Configuration

```bash
# Prevent abuse with rate limiting
RATE_LIMIT_REQUESTS=10          # Requests per window
RATE_LIMIT_WINDOW=60            # Window in seconds
RATE_LIMIT_BURST=20             # Burst capacity

# Cost-based limiting
CLAUDE_MAX_COST_PER_USER=10.0   # Max cost per user in USD
```

### Development Setup

For development work:

```bash
# Development-specific settings
DEBUG=true
DEVELOPMENT_MODE=true
LOG_LEVEL=DEBUG
ENVIRONMENT=development

# More lenient rate limits for testing
RATE_LIMIT_REQUESTS=100
CLAUDE_TIMEOUT_SECONDS=600
```

## Troubleshooting

### Common Setup Issues

#### Bot doesn't respond
```bash
# Check your bot token
echo $TELEGRAM_BOT_TOKEN

# Verify user ID is correct
# Message @userinfobot to get your ID

# Check bot logs
make run-debug
```

#### Claude authentication issues

**For SDK + CLI Auth:**
```bash
# Check CLI authentication
claude auth status

# Should show: "✓ You are authenticated"
# If not, run: claude auth login
```

**For SDK + API Key:**
```bash
# Verify API key is set
echo $ANTHROPIC_API_KEY

# Should start with: sk-ant-api03-
# Get a new key from: https://console.anthropic.com/
```

**For CLI Mode:**
```bash
# Check CLI installation
claude --version

# Check authentication
claude auth status

# Test CLI works
claude "Hello, can you help me?"
```

#### Permission errors
```bash
# Check approved directory exists and is accessible
ls -la /path/to/your/projects

# Verify bot process has read/write permissions
# The directory should be owned by the user running the bot
```

### Performance Optimization

#### For SDK Mode
```bash
# Optimal settings for SDK integration
USE_SDK=true
CLAUDE_TIMEOUT_SECONDS=300
CLAUDE_MAX_TURNS=20
```

#### For CLI Mode
```bash
# If you must use CLI mode, optimize these settings
USE_SDK=false
CLAUDE_TIMEOUT_SECONDS=450      # Higher timeout for subprocess overhead
CLAUDE_MAX_TURNS=10             # Lower turns to reduce subprocess calls
```

### Monitoring and Logging

#### Enable detailed logging
```bash
LOG_LEVEL=DEBUG
DEBUG=true

# Run with debug output
make run-debug
```

#### Monitor usage, costs and traces
```bash
# Check usage in Telegram
/status

# Monitor logs for cost tracking
tail -f logs/bot.log | grep -i cost

# Enable OpenTelemetry/Uptrace integration
TELEMETRY_ENABLED=true
TELEMETRY_SERVICE_NAME=claude-code-telegram

# Point OTEL exporter to Uptrace
OTEL_EXPORTER_OTLP_ENDPOINT=http://uptrace:4317
OTEL_EXPORTER_OTLP_HEADERS="uptrace-dsn=YOUR_UPTRACE_DSN"
```

## Production Deployment

### Environment-specific settings

```bash
# Production configuration
ENVIRONMENT=production
DEBUG=false
LOG_LEVEL=INFO
DEVELOPMENT_MODE=false

# Stricter rate limits
RATE_LIMIT_REQUESTS=5
CLAUDE_MAX_COST_PER_USER=5.0
SESSION_TIMEOUT_HOURS=12

# Enable monitoring
ENABLE_TELEMETRY=true
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project
```

### Database configuration

```bash
# For production, use a persistent database location
DATABASE_URL=sqlite:///var/lib/claude-telegram/bot.db

# Or use PostgreSQL for high-scale deployments
# DATABASE_URL=postgresql://user:pass@localhost/claude_telegram
```

### Security hardening

```bash
# Enable token authentication for additional security
ENABLE_TOKEN_AUTH=true
AUTH_TOKEN_SECRET=your-very-secure-secret-key

# Restrict to specific users only
ALLOWED_USERS=123456789,987654321

# Use a restricted project directory
APPROVED_DIRECTORY=/opt/projects
```

## Getting Help

- **Documentation**: Check the main [README.md](../README.md)
- **Configuration**: See [configuration.md](configuration.md) for all options
- **Development**: See [development.md](development.md) for development setup
- **Issues**: [Open an issue](https://github.com/yourusername/claude-code-telegram/issues)
- **Security**: See [SECURITY.md](../SECURITY.md) for security concerns