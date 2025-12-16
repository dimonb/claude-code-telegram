---
name: uptrace-otel-integration
overview: Add OpenTelemetry/Uptrace telemetry (logs + traces) to the Claude Code Telegram bot and instrument key operations with explicit spans.
todos: []
---

# План внедрения Uptrace/OTEL телеметрии

## 1. Зависимости и базовая конфигурация

- **Добавить OTEL-зависимости** в `pyproject.toml` проекта:
- `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc` (минимум для спанов и логов).
- При необходимости в будущем: `opentelemetry-instrumentation-requests`, `opentelemetry-instrumentation-aiohttp-client` и т.п.
- **Расширить настройки** в [`src/config/settings.py`](src/config/settings.py):
- Добавить поля: `telemetry_enabled: bool = False`, `telemetry_service_name: str = "claude-code-telegram"`, `telemetry_json_log: bool = True`, `telemetry_log_level: str = "INFO"`.
- Опционально: словарь `telemetry_log_custom_levels: dict[str, str] `по аналогии с `finbot`.
- **Документировать переменные окружения** в [`docs/configuration.md`](docs/configuration.md)/[`docs/setup.md`](docs/setup.md):
- Как включать телеметрию (`TELEMETRY_ENABLED`, `TELEMETRY_SERVICE_NAME`, `TELEMETRY_JSON_LOG`).

## 2. Модуль телеметрии (логирование + OTLP)

- **Создать модуль телеметрии**, например [`src/infra/telemetry/otel.py`](src/infra/telemetry/otel.py) (или `src/utils/telemetry.py`), по мотивам `FINLAB/finbot/app/infra/telemetry/otel.py`:
- Функция `configure_logging(settings: Settings) -> None`:
- На основе текущей логики `setup_logging` в [`src/main.py`](src/main.py) сконфигурировать `structlog` + корневой `logging`.
- Если `settings.telemetry_enabled`:
- Создать `Resource` с атрибутами `service.name`, `service.version`, `deployment.environment`.
- Создать `LoggerProvider` + `OTLPLogExporter` + `BatchLogRecordProcessor`.
- Добавить `LoggingHandler` к корневому логгеру, чтобы структурированные логи уходили в OTEL (Uptrace).
- Функция `configure_tracing(settings: Settings) -> None`:
- При `telemetry_enabled` создать `TracerProvider(resource=...)` и назначить его через `trace.set_tracer_provider`.
- Создать `OTLPSpanExporter` + `BatchSpanProcessor` и повесить на провайдера.
- Пока без агрессивной автоинструментации, достаточно базового провайдера.

## 3. Встраивание телеметрии в lifecycle бота

- **Обновить точку входа** [`src/main.py`](src/main.py):
- Загрузить `Settings` до конфигурации логов (либо использовать уже имеющийся `load_config`).
- Заменить/расширить текущий `setup_logging(debug=...)` на вызов `configure_logging(config)` из нового модуля.
- После логирования вызвать `configure_tracing(config)`.
- Убедиться, что это происходит до создания `ClaudeIntegration` и `ClaudeCodeBot`, чтобы все их логи и спаны шли через OTEL.
- **Согласовать существующий `setup_logging`**:
- Либо удалить/заменить его на thin-wrapper вокруг нового `configure_logging`, либо перенести туда новую реализацию, чтобы избежать дублирования конфигурации.

## 4. Явные спаны для ключевых операций

- **Инструментировать обработку апдейтов Telegram**:
- В [`src/bot/handlers/message.py`](src/bot/handlers/message.py) в `handle_text_message`:
- Получить `tracer = trace.get_tracer("claude-code-telegram")` (через новый модуль или напрямую из `opentelemetry.trace`).
- Обернуть основную часть обработки (rate-limit, вызов Claude, форматирование ответа, логирование) в `with tracer.start_as_current_span("telegram.handle_text"):`.
- В span добавить атрибуты: `telegram.user_id`, `telegram.chat_id`, `telegram.message_length`, `working_directory`.
- В блоках `except` для ошибок Claude/форматтера вызывать `span.record_exception(e)` и ставить `span.set_status(Status(StatusCode.ERROR, ...))`.
- Аналогично для `handle_document` и `handle_photo` (спаны `telegram.handle_document`, `telegram.handle_photo` + атрибуты типа `file_name`, `file_size`, `has_caption`).
- **Инструментировать интеграцию с Claude**:
- В [`src/claude/facade.py`](src/claude/facade.py) внутри `ClaudeIntegration.run_command`:
- Обернуть вызов `_execute_with_fallback` и работу с `session_manager` в `with tracer.start_as_current_span("claude.run_command"):`.
- Атрибуты: `user_id`, `working_directory`, `has_session_id`, `prompt_length`, флаги `use_sdk`/`use_subprocess`.
- В случае ошибок `ClaudeProcessError`/`ClaudeTimeoutError`/`ClaudeToolValidationError` — `record_exception` и `StatusCode.ERROR`.
- **Инструментировать низкоуровневые команды Claude (опционально)**:
- В [`src/claude/integration.py`](src/claude/integration.py) `ClaudeProcessManager.execute_command`:
- Создать span `claude.subprocess.execute` вокруг старта процесса и `_handle_process_output`.
- Атрибуты: `cwd`, `session_id`, `continue_session`, `cmd_args` (усечённо).
- В [`src/claude/sdk_integration.py`](src/claude/sdk_integration.py) `ClaudeSDKManager.execute_command`:
- Span `claude.sdk.execute` с атрибутами `session_id`, `continue_session`, `use_allowed_tools`.

## 5. Интеграция с Uptrace (на уровне окружения)

- **Описать стандартные OTEL‑переменные** в документации и deployment:
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://uptrace:4317` (или твой адрес).
- `OTEL_EXPORTER_OTLP_HEADERS="uptrace-dsn=..."` по аналогии с `FINLAB/deployment/aktar/base/config.env`.
- **(Опционально) Добавить project‑специфические префиксы** в `.env`/docs, если хочешь отделить этот бот от других сервисов (например, `CLAUDEBOT_OTEL_EXPORTER_OTLP_ENDPOINT`, как `FINBOT_OTEL_...` в `finbot`).

## 6. Тестирование и валидация

- **Локальная проверка**:
- Запустить бота с включённой телеметрией и локальным/тестовым Uptrace, проверить что:
- в Uptrace появляются спаны `telegram.handle_text`, `claude.run_command` и т.д.; 
- логи уходят через OTLP и коррелируются со спанами.
- **Нагрузочный smoke‑test**:
- Прогнать несколько типичных сценариев (обычное сообщение, длинный запрос, загрузка файла, ошибка Claude, rate-limit), убедиться, что ошибки и таймауты отражаются в трейсах и логах с корректным `StatusCode.ERROR` и `exception`.
- **Документация**:
- Обновить [`docs/implementation-summary.md`](docs/implementation-summary.md) или аналогичный файл кратким описанием телеметрии: что включено, какие переменные, где смотреть в Uptrace.