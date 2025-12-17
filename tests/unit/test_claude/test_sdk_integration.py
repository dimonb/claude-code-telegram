"""Test Claude SDK integration."""

import asyncio
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.exceptions import ClaudeProcessError, ClaudeTimeoutError
from src.claude.sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from src.config.settings import Settings


class MockClaudeSDKClient:
    """Mock ClaudeSDKClient for testing."""

    def __init__(self, options: Any = None, message_generator: Optional[Callable] = None):
        """Initialize mock client.

        Args:
            options: ClaudeAgentOptions (ignored in mock)
            message_generator: Function that returns an async generator of messages
        """
        self.options = options
        self.message_generator = message_generator
        self._prompt = None

    async def __aenter__(self):
        """Enter async context manager."""
        return self

    async def __aexit__(self, *args):
        """Exit async context manager."""
        pass

    async def query(self, prompt: str):
        """Store the query prompt."""
        self._prompt = prompt

    async def receive_response(self) -> AsyncIterator[Any]:
        """Yield messages from the message generator."""
        if self.message_generator:
            async for message in self.message_generator():
                yield message


class TestClaudeSDKManager:
    """Test Claude SDK manager."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config without API key."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            use_sdk=True,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_sdk_manager_initialization_with_api_key(self, tmp_path):
        """Test SDK manager initialization with API key."""
        config_with_key = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            anthropic_api_key="test-api-key",
            use_sdk=True,
            claude_timeout_seconds=2,
        )

        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            manager = ClaudeSDKManager(config_with_key)
            assert os.environ.get("ANTHROPIC_API_KEY") == "test-api-key"
            assert manager.active_sessions == {}
        finally:
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key
            elif "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

    async def test_sdk_manager_initialization_without_api_key(self, config):
        """Test SDK manager initialization without API key (uses CLI auth)."""
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            if "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

            manager = ClaudeSDKManager(config)
            assert config.anthropic_api_key_str is None
            assert manager.active_sessions == {}
        finally:
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key

    async def test_execute_command_success(self, sdk_manager):
        """Test successful command execution."""
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

        async def mock_message_generator():
            yield AssistantMessage(content=[TextBlock(text="Test response")], model="claude-sonnet-4-20250514")
            yield ResultMessage(
                subtype="success",
                duration_ms=1000,
                duration_api_ms=800,
                is_error=False,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0.05,
                result="Success",
            )

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="test-session",
            )

        assert isinstance(response, ClaudeResponse)
        assert response.session_id == "test-session"
        assert response.duration_ms >= 0
        assert not response.is_error
        assert response.cost == 0.05

    async def test_execute_command_with_streaming(self, sdk_manager):
        """Test command execution with streaming callback."""
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

        stream_updates = []

        async def stream_callback(update: StreamUpdate):
            stream_updates.append(update)

        async def mock_message_generator():
            yield AssistantMessage(content=[TextBlock(text="Test response")], model="claude-sonnet-4-20250514")
            yield ResultMessage(
                subtype="success",
                duration_ms=1000,
                duration_api_ms=800,
                is_error=False,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0.05,
                result="Success",
            )

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                stream_callback=stream_callback,
            )

        assert len(stream_updates) > 0
        assert any(update.type == "assistant" for update in stream_updates)

    async def test_execute_command_timeout(self, sdk_manager, tmp_path):
        """Test command execution timeout."""

        async def mock_hanging_generator():
            await asyncio.sleep(5)
            yield

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_hanging_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=tmp_path,
                )

    async def test_session_management(self, sdk_manager):
        """Test session management."""
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        session_id = "test-session"
        messages = [AssistantMessage(content=[TextBlock(text="test")], model="claude-sonnet-4-20250514")]

        sdk_manager._update_session(session_id, messages)

        assert session_id in sdk_manager.active_sessions
        session_data = sdk_manager.active_sessions[session_id]
        assert session_data["messages"] == messages

    async def test_kill_all_processes(self, sdk_manager):
        """Test killing all processes (clearing sessions)."""
        sdk_manager.active_sessions["session1"] = {"test": "data"}
        sdk_manager.active_sessions["session2"] = {"test": "data2"}

        assert len(sdk_manager.active_sessions) == 2

        await sdk_manager.kill_all_processes()

        assert len(sdk_manager.active_sessions) == 0

    def test_get_active_process_count(self, sdk_manager):
        """Test getting active process count."""
        assert sdk_manager.get_active_process_count() == 0

        sdk_manager.active_sessions["session1"] = {"test": "data"}
        sdk_manager.active_sessions["session2"] = {"test": "data2"}

        assert sdk_manager.get_active_process_count() == 2


class TestClaudeSDKErrorHandling:
    """Test error handling in Claude SDK integration."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            use_sdk=True,
            claude_timeout_seconds=5,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_limit_reached_error(self, sdk_manager):
        """Test handling of usage limit reached error."""
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

        async def mock_message_generator():
            yield AssistantMessage(content=[TextBlock(text="Processing...")], model="claude-sonnet-4-20250514")
            yield ResultMessage(
                subtype="success",
                duration_ms=1000,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0,
                result="Limit reached · resets 8pm (Asia/Jerusalem)",
            )

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        error_msg = str(exc_info.value)
        assert "Usage Limit Reached" in error_msg
        assert "8pm" in error_msg
        assert "Asia/Jerusalem" in error_msg

    async def test_limit_reached_error_without_timezone(self, sdk_manager):
        """Test handling of limit reached error without timezone."""
        from claude_agent_sdk.types import ResultMessage

        async def mock_message_generator():
            yield ResultMessage(
                subtype="success",
                duration_ms=1000,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0,
                result="Limit reached · resets 9am",
            )

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        error_msg = str(exc_info.value)
        assert "Usage Limit Reached" in error_msg
        assert "9am" in error_msg

    async def test_generic_error_in_result(self, sdk_manager):
        """Test handling of generic error in result message."""
        from claude_agent_sdk.types import ResultMessage

        async def mock_message_generator():
            yield ResultMessage(
                subtype="error",
                duration_ms=500,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0,
                result="Some unexpected error occurred",
            )

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        error_msg = str(exc_info.value)
        assert "Some unexpected error occurred" in error_msg

    async def test_empty_error_in_result(self, sdk_manager):
        """Test handling of empty error in result message."""
        from claude_agent_sdk.types import ResultMessage

        async def mock_message_generator():
            yield ResultMessage(
                subtype="error",
                duration_ms=500,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0,
                result="",
            )

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert exc_info.value is not None

    async def test_json_decode_error_with_result_error(self, sdk_manager):
        """Test that result error takes precedence over JSON decode error."""
        from claude_agent_sdk.types import ResultMessage
        from claude_agent_sdk._errors import CLIJSONDecodeError

        messages_received = []

        async def mock_message_generator():
            msg = ResultMessage(
                subtype="success",
                duration_ms=1000,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0,
                result="Limit reached · resets 10pm",
            )
            messages_received.append(msg)
            yield msg
            raise CLIJSONDecodeError("invalid json", Exception("Extra data"))

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        error_msg = str(exc_info.value)
        assert "Limit reached" in error_msg or "Usage Limit" in error_msg

    async def test_exception_group_with_result_error(self, sdk_manager):
        """Test handling of ExceptionGroup when result has error."""
        from claude_agent_sdk.types import ResultMessage

        async def mock_message_generator():
            yield ResultMessage(
                subtype="success",
                duration_ms=1000,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
                total_cost_usd=0,
                result="Limit reached · resets 11pm (UTC)",
            )
            raise ExceptionGroup("test errors", [ValueError("inner error")])

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        error_msg = str(exc_info.value)
        assert "Limit reached" in error_msg or "Usage Limit" in error_msg

    async def test_claude_process_error_not_wrapped(self, sdk_manager):
        """Test that ClaudeProcessError is not double-wrapped."""
        original_error = ClaudeProcessError("Original error message")

        async def mock_message_generator():
            raise original_error
            yield

        def mock_client_factory(options):
            return MockClaudeSDKClient(options, mock_message_generator)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_client_factory):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "Original error message" in str(exc_info.value)
        assert "Unexpected error:" not in str(exc_info.value)


class TestClaudeIntegrationErrorHandling:
    """Test error handling in Claude integration (subprocess)."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            use_sdk=False,
            claude_timeout_seconds=5,
        )

    @pytest.fixture
    def process_manager(self, config):
        """Create process manager."""
        from src.claude.integration import ClaudeProcessManager

        return ClaudeProcessManager(config)

    async def test_result_with_is_error_handled(self, process_manager):
        """Test that result with is_error=True is properly handled."""
        from unittest.mock import AsyncMock

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.stderr.read = AsyncMock(return_value=b"")

        result_json = (
            '{"type": "result", "is_error": true, '
            '"result": "Limit reached · resets 5pm (UTC)", '
            '"session_id": "test", "cost_usd": 0, "duration_ms": 100, "num_turns": 1}'
        )

        async def mock_read_stream(stream):
            yield result_json

        with (
            patch.object(process_manager, "_start_process", return_value=mock_process),
            patch.object(
                process_manager, "_read_stream_bounded", side_effect=mock_read_stream
            ),
        ):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await process_manager.execute_command(
                    prompt="test",
                    working_directory=Path("/test"),
                )

        error_msg = str(exc_info.value)
        assert "Usage Limit Reached" in error_msg
        assert "5pm" in error_msg

    async def test_session_not_found_error(self, process_manager):
        """Test handling of session not found error."""
        from unittest.mock import AsyncMock

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)
        mock_process.stderr.read = AsyncMock(
            return_value=b"No conversation found with session ID: abc-123"
        )

        async def mock_read_stream(stream):
            return
            yield

        with (
            patch.object(process_manager, "_start_process", return_value=mock_process),
            patch.object(
                process_manager, "_read_stream_bounded", side_effect=mock_read_stream
            ),
        ):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await process_manager.execute_command(
                    prompt="test",
                    working_directory=Path("/test"),
                    session_id="abc-123",
                    continue_session=True,
                )

        error_msg = str(exc_info.value)
        assert "Session Not Found" in error_msg

    async def test_empty_result_with_is_error_still_raises(self, process_manager):
        """Test that empty result with is_error=True still raises error."""
        from unittest.mock import AsyncMock

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.stderr.read = AsyncMock(return_value=b"")

        result_json = (
            '{"type": "result", "is_error": true, '
            '"result": "", '
            '"session_id": "test", "cost_usd": 0, "duration_ms": 100, "num_turns": 1}'
        )

        async def mock_read_stream(stream):
            yield result_json

        with (
            patch.object(process_manager, "_start_process", return_value=mock_process),
            patch.object(
                process_manager, "_read_stream_bounded", side_effect=mock_read_stream
            ),
        ):
            with pytest.raises(ClaudeProcessError) as exc_info:
                await process_manager.execute_command(
                    prompt="test",
                    working_directory=Path("/test"),
                )

        assert exc_info.value is not None


class TestMessageFormatting:
    """Test error message formatting."""

    def test_format_limit_reached_error(self):
        """Test formatting of limit reached error message."""
        from src.bot.handlers.message import _format_error_message

        error = "Claude error: Limit reached · resets 8pm (Asia/Jerusalem)"
        formatted = _format_error_message(error)

        assert "Usage Limit Reached" in formatted
        assert "8pm" in formatted
        assert "Asia/Jerusalem" in formatted

    def test_format_limit_reached_without_timezone(self):
        """Test formatting of limit reached without timezone."""
        from src.bot.handlers.message import _format_error_message

        error = "Limit reached · resets 9am"
        formatted = _format_error_message(error)

        assert "Usage Limit Reached" in formatted
        assert "9am" in formatted

    def test_format_session_not_found_error(self):
        """Test formatting of session not found error."""
        from src.bot.handlers.message import _format_error_message

        error = "No conversation found with session ID: test-123"
        formatted = _format_error_message(error)

        assert "Session Not Found" in formatted
        assert "/new" in formatted

    def test_already_formatted_message_not_changed(self):
        """Test that already formatted messages are not changed."""
        from src.bot.handlers.message import _format_error_message

        formatted_msg = (
            "⏱️ **Claude AI Usage Limit Reached**\n\n" "Already formatted message"
        )
        result = _format_error_message(formatted_msg)

        assert result == formatted_msg
