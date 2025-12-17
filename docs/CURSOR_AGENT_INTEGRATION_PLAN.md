# План интеграции cursor-agent как альтернативного AI-agent

## 1. Анализ текущей архитектуры

### Существующие компоненты

| Файл | Класс | Описание |
|------|-------|----------|
| `src/claude/sdk_integration.py` | `ClaudeSDKManager` | Интеграция через Python SDK `claude-agent-sdk` |
| `src/claude/integration.py` | `ClaudeProcessManager` | Интеграция через subprocess `claude` CLI |
| `src/claude/facade.py` | `ClaudeIntegration` | Фасад для работы с AI агентами |

### Принцип работы (без fallback)

> **Важно:** Fallback механизм НЕ используется. Выбор AI-агента осуществляется **только через конфигурацию**.
> Если выбранный агент недоступен, возвращается ошибка с понятным сообщением.

Приоритет выбора агента в конфигурации:
1. `USE_CURSOR_AGENT=true` → CursorAgentManager
2. `USE_SDK=true` → ClaudeSDKManager  
3. По умолчанию → ClaudeProcessManager

### Модели данных

- **`ClaudeResponse`** — финальный ответ:
  - `content`, `session_id`, `cost`, `duration_ms`, `num_turns`, `tools_used`
- **`StreamUpdate`** — стриминговое обновление:
  - `type`, `content`, `tool_calls`, `metadata`, `progress`

---

## 2. Формат вывода cursor-agent

### Команда запуска

```bash
cursor-agent -f --approve-mcps --print --output-format stream-json --stream-partial-output \
  --workspace <working_directory> \
  [--model <model>] \
  [--resume <session_id>] \
  "<prompt>"
```

### Типы сообщений (stream-json)

| Type | Subtype | Описание |
|------|---------|----------|
| `system` | `init` | Инициализация сессии (apiKeySource, cwd, model, permissionMode) |
| `user` | — | Сообщение пользователя |
| `thinking` | `delta` / `completed` | Процесс "мышления" модели (стриминг) |
| `assistant` | — | Ответ ассистента (частичный стриминг) |
| `tool_call` | `started` / `completed` | Вызов инструмента (grepToolCall, readToolCall, etc.) |
| `result` | `success` / `error` | Финальный результат (duration_ms, is_error, result) |

### Примеры сообщений

```json
// Инициализация
{"type":"system","subtype":"init","apiKeySource":"login","cwd":"/path/to/project","session_id":"uuid","model":"Composer 1","permissionMode":"default"}

// Сообщение пользователя
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"prompt"}]},"session_id":"uuid"}

// Мышление (стриминг)
{"type":"thinking","subtype":"delta","text":"...","session_id":"uuid","timestamp_ms":123456}
{"type":"thinking","subtype":"completed","session_id":"uuid","timestamp_ms":123456}

// Ответ ассистента (частичный стриминг)
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"partial text"}]},"session_id":"uuid","timestamp_ms":123456}

// Вызов инструмента
{"type":"tool_call","subtype":"started","call_id":"tool_xxx","tool_call":{"grepToolCall":{"args":{...}}},"session_id":"uuid"}
{"type":"tool_call","subtype":"completed","call_id":"tool_xxx","tool_call":{"grepToolCall":{"args":{...},"result":{...}}},"session_id":"uuid"}

// Финальный результат
{"type":"result","subtype":"success","duration_ms":47845,"is_error":false,"result":"...","session_id":"uuid"}
```

---

## 3. План работ

### Фаза 1: Создание CursorAgentManager

**Файл:** `src/claude/cursor_agent_integration.py`

**Задачи:**

1. [ ] Создать класс `CursorAgentManager` по аналогии с `ClaudeProcessManager`
2. [ ] Реализовать методы:
   - `execute_command()` — запуск cursor-agent subprocess
   - `_build_command()` — построение команды с аргументами
   - `_start_process()` — запуск subprocess
   - `_handle_process_output()` — обработка вывода с коллбэками
3. [ ] Реализовать парсинг JSON stream сообщений:
   - `_parse_stream_message()` — основной парсер
   - `_parse_system_message()` — обработка init
   - `_parse_thinking_message()` — обработка thinking delta/completed
   - `_parse_assistant_message()` — обработка assistant
   - `_parse_tool_call_message()` — обработка tool_call started/completed
   - `_parse_result_message()` — обработка result
4. [ ] Маппинг cursor-agent сообщений → `StreamUpdate`
5. [ ] Формирование финального `ClaudeResponse`

### Фаза 2: Расширение конфигурации

**Файл:** `src/config/settings.py`

**Задачи:**

1. [ ] Добавить настройки:

```python
# Cursor Agent settings
cursor_agent_binary_path: Optional[str] = Field(
    None, 
    description="Path to cursor-agent binary"
)
use_cursor_agent: bool = Field(
    False, 
    description="Use cursor-agent instead of Claude SDK/CLI"
)
cursor_agent_model: Optional[str] = Field(
    None, 
    description="Model for cursor-agent (e.g., sonnet-4, gpt-5)"
)
cursor_agent_approve_mcps: bool = Field(
    True, 
    description="Auto-approve MCP servers in cursor-agent"
)
cursor_agent_force_mode: bool = Field(
    True, 
    description="Force allow commands in cursor-agent (-f flag)"
)
```

### Фаза 3: Интеграция в фасад

**Файл:** `src/claude/facade.py`

**Задачи:**

1. [ ] Импортировать `CursorAgentManager`
2. [ ] Добавить инициализацию в `__init__`:

```python
# Initialize manager based on configuration (NO FALLBACK - explicit choice)
if config.use_cursor_agent:
    self.manager = CursorAgentManager(config)
    logger.info("Using cursor-agent for AI integration")
elif config.use_sdk:
    self.manager = ClaudeSDKManager(config)
    logger.info("Using Claude SDK for AI integration")
else:
    self.manager = ClaudeProcessManager(config)
    logger.info("Using Claude CLI subprocess for AI integration")
```

3. [ ] Убрать `_execute_with_fallback()` — заменить на простой `_execute()` без fallback логики
4. [ ] Добавить понятные сообщения об ошибках если агент недоступен

### Фаза 4: Парсер сообщений (опционально выделить в отдельный файл)

**Файл:** `src/claude/cursor_agent_parser.py` (новый, опционально)

**Задачи:**

1. [ ] Парсинг вложенных tool_call структур:
   - `grepToolCall`
   - `readToolCall`
   - `editToolCall`
   - `semSearchToolCall`
   - `listToolCall`
   - `shellToolCall`
2. [ ] Агрегация частичных `assistant` сообщений
3. [ ] Обработка `thinking` сообщений (опциональная передача в стрим)
4. [ ] Извлечение метаданных из `result`

### Фаза 5: Кнопки с командами из `.claude/commands`

**Описание:**

Когда пользователь находится в проекте, показывать inline-кнопки с доступными командами из директории `.claude/commands/`. Каждый `.md` файл в этой директории представляет команду.

**Структура `.claude/commands/`:**

```
project/
└── .claude/
    └── commands/
        ├── release-build.md      → кнопка "release-build"
        ├── release-changes.md    → кнопка "release-changes"
        ├── release-ticket.md     → кнопка "release-ticket"
        └── release-upcoming.md   → кнопка "release-upcoming"
```

**Файлы для изменения:**

1. **`src/bot/features/project_commands.py`** (новый файл)

**Задачи:**

1. [ ] Создать функцию `get_project_commands(working_directory: Path) -> List[ProjectCommand]`:
   ```python
   @dataclass
   class ProjectCommand:
       name: str           # Имя команды (без .md)
       file_path: Path     # Полный путь к файлу
       description: str    # Первая строка из файла (заголовок)
   
   def get_project_commands(working_directory: Path) -> List[ProjectCommand]:
       """Scan .claude/commands/ directory for available commands."""
       commands_dir = working_directory / ".claude" / "commands"
       if not commands_dir.exists():
           return []
       
       commands = []
       for md_file in commands_dir.glob("*.md"):
           name = md_file.stem  # filename without .md
           # Read first line for description
           with open(md_file, 'r') as f:
               first_line = f.readline().strip()
               description = first_line.lstrip('#').strip()
           commands.append(ProjectCommand(
               name=name,
               file_path=md_file,
               description=description
           ))
       return sorted(commands, key=lambda c: c.name)
   ```

2. [ ] Создать функцию `read_command_content(command: ProjectCommand) -> str`:
   ```python
   def read_command_content(command: ProjectCommand) -> str:
       """Read the full content of a command file."""
       return command.file_path.read_text()
   ```

3. [ ] Создать функцию `build_commands_keyboard(commands: List[ProjectCommand]) -> InlineKeyboardMarkup`:
   ```python
   def build_commands_keyboard(commands: List[ProjectCommand]) -> InlineKeyboardMarkup:
       """Build inline keyboard with project commands."""
       buttons = []
       for cmd in commands:
           buttons.append([
               InlineKeyboardButton(
                   text=f"/{cmd.name}",
                   callback_data=f"pcmd:{cmd.name}"  # project command
               )
           ])
       return InlineKeyboardMarkup(buttons)
   ```

2. **`src/bot/handlers/command.py`**

**Задачи:**

4. [ ] Добавить команду `/commands` для показа доступных команд проекта:
   ```python
   async def commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
       """Show available project commands from .claude/commands/"""
       working_dir = context.bot_data.get("working_directory")
       commands = get_project_commands(working_dir)
       
       if not commands:
           await update.message.reply_text(
               "📁 No project commands found.\n\n"
               "Create commands in `.claude/commands/*.md`"
           )
           return
       
       keyboard = build_commands_keyboard(commands)
       await update.message.reply_text(
           f"📋 **Available Project Commands** ({len(commands)}):\n\n"
           + "\n".join(f"• `/{c.name}` — {c.description}" for c in commands),
           reply_markup=keyboard,
           parse_mode="Markdown"
       )
   ```

3. **`src/bot/handlers/callback.py`**

**Задачи:**

5. [ ] Добавить обработчик callback для кнопок команд:
   ```python
   async def handle_project_command_callback(
       update: Update, 
       context: ContextTypes.DEFAULT_TYPE
   ) -> None:
       """Handle project command button press."""
       query = update.callback_query
       await query.answer()
       
       # Extract command name from callback_data "pcmd:command-name"
       command_name = query.data.split(":", 1)[1]
       working_dir = context.bot_data.get("working_directory")
       
       commands = get_project_commands(working_dir)
       command = next((c for c in commands if c.name == command_name), None)
       
       if not command:
           await query.edit_message_text(f"❌ Command `{command_name}` not found")
           return
       
       # Read command content and execute
       prompt = read_command_content(command)
       
       # Show "executing" message
       await query.edit_message_text(f"⏳ Executing `/{command_name}`...")
       
       # Execute via Claude integration (as if user sent the prompt)
       # ... delegate to message handler or claude integration
   ```

4. **`src/bot/core.py`**

**Задачи:**

6. [ ] Зарегистрировать новый handler для `/commands`
7. [ ] Зарегистрировать callback handler для `pcmd:*` pattern

**UI Flow:**

```
User: /commands

Bot: 📋 **Available Project Commands** (4):

• `/release-build` — Release Build
• `/release-changes` — Release Changes  
• `/release-ticket` — Release Ticket
• `/release-upcoming` — Release Upcoming

[/release-build] [/release-changes]
[/release-ticket] [/release-upcoming]

User: *clicks /release-upcoming button*

Bot: ⏳ Executing `/release-upcoming`...
Bot: *streams response from AI agent*
```

---

### Фаза 6: Тестирование

**Файлы:**
- `tests/unit/test_claude/test_cursor_agent_integration.py`
- `tests/unit/test_bot/test_project_commands.py` (новый)

**Задачи для cursor-agent:**

1. [ ] Unit-тесты для парсинга JSON stream:
   - test_parse_system_init
   - test_parse_thinking_delta
   - test_parse_thinking_completed
   - test_parse_assistant_message
   - test_parse_tool_call_started
   - test_parse_tool_call_completed
   - test_parse_result_success
   - test_parse_result_error
2. [ ] Тесты построения команды:
   - test_build_command_basic
   - test_build_command_with_session
   - test_build_command_with_model
3. [ ] Тесты маппинга в StreamUpdate/ClaudeResponse
4. [ ] Mock-тесты subprocess взаимодействия
5. [ ] Тесты обработки ошибок

**Задачи для project commands:**

6. [ ] Тесты сканирования `.claude/commands/`:
   - test_get_project_commands_empty_dir
   - test_get_project_commands_with_files
   - test_get_project_commands_no_claude_dir
7. [ ] Тесты чтения контента команд:
   - test_read_command_content
   - test_read_command_description
8. [ ] Тесты построения клавиатуры:
   - test_build_commands_keyboard
   - test_build_commands_keyboard_empty
9. [ ] Тесты callback handler:
   - test_handle_project_command_callback
   - test_handle_project_command_not_found

### Фаза 7: Документация

**Задачи:**

1. [ ] Обновить `CLAUDE.md`:
   - Добавить описание cursor-agent режима
   - Добавить сравнение трёх режимов
2. [ ] Обновить `.env.example`:
   - Добавить `CURSOR_AGENT_BINARY_PATH`
   - Добавить `USE_CURSOR_AGENT`
   - Добавить `CURSOR_AGENT_MODEL`
3. [ ] Добавить примеры конфигурации в README

---

## 4. Технические детали реализации

### 4.1 Структура CursorAgentManager

```python
class CursorAgentManager:
    """Manage cursor-agent subprocess execution."""
    
    def __init__(self, config: Settings):
        self.config = config
        self.active_processes: Dict[str, Process] = {}
        
    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        ...
        
    def _build_command(
        self, 
        prompt: str, 
        working_directory: Path,
        session_id: Optional[str], 
        continue_session: bool
    ) -> List[str]:
        cmd = [self.config.cursor_agent_binary_path or "cursor-agent"]
        
        # Force mode
        if self.config.cursor_agent_force_mode:
            cmd.append("-f")
            
        # Auto-approve MCPs
        if self.config.cursor_agent_approve_mcps:
            cmd.append("--approve-mcps")
            
        # Print mode for headless
        cmd.append("--print")
        
        # JSON streaming output
        cmd.extend(["--output-format", "stream-json"])
        cmd.append("--stream-partial-output")
        
        # Workspace
        cmd.extend(["--workspace", str(working_directory)])
        
        # Model
        if self.config.cursor_agent_model:
            cmd.extend(["--model", self.config.cursor_agent_model])
            
        # Resume session
        if continue_session and session_id:
            cmd.extend(["--resume", session_id])
            
        # Prompt
        cmd.append(prompt)
        
        return cmd
```

### 4.2 Маппинг типов сообщений

```python
def _parse_stream_message(self, msg: Dict) -> Optional[StreamUpdate]:
    msg_type = msg.get("type")
    
    if msg_type == "system":
        return self._parse_system_message(msg)
    elif msg_type == "user":
        return self._parse_user_message(msg)
    elif msg_type == "thinking":
        return self._parse_thinking_message(msg)
    elif msg_type == "assistant":
        return self._parse_assistant_message(msg)
    elif msg_type == "tool_call":
        return self._parse_tool_call_message(msg)
    elif msg_type == "result":
        return None  # Handle separately for final response
    
    logger.debug("Unknown message type", msg_type=msg_type)
    return None
```

### 4.3 Обработка tool_call

```python
TOOL_CALL_TYPES = [
    "grepToolCall",
    "readToolCall", 
    "editToolCall",
    "semSearchToolCall",
    "listToolCall",
    "shellToolCall",
    "writeToolCall",
]

def _parse_tool_call_message(self, msg: Dict) -> StreamUpdate:
    tool_call_data = msg.get("tool_call", {})
    subtype = msg.get("subtype")  # started/completed
    call_id = msg.get("call_id")
    
    # Find the tool type
    tool_name = None
    tool_args = {}
    tool_result = None
    
    for tool_type in TOOL_CALL_TYPES:
        if tool_type in tool_call_data:
            tool_info = tool_call_data[tool_type]
            tool_name = tool_type.replace("ToolCall", "")
            tool_args = tool_info.get("args", {})
            if subtype == "completed":
                tool_result = tool_info.get("result")
            break
    
    return StreamUpdate(
        type="tool_call" if subtype == "started" else "tool_result",
        metadata={
            "subtype": subtype,
            "call_id": call_id,
            "tool_name": tool_name,
        },
        tool_calls=[{
            "name": tool_name,
            "input": tool_args,
            "id": call_id,
            "result": tool_result,
        }] if tool_name else None,
        timestamp=str(msg.get("timestamp_ms")),
        session_context={"session_id": msg.get("session_id")},
    )
```

### 4.4 Формирование ClaudeResponse

```python
def _parse_result(self, result: Dict, messages: List[Dict]) -> ClaudeResponse:
    # Extract assistant content from messages
    content_parts = []
    tools_used = []
    
    for msg in messages:
        if msg.get("type") == "assistant":
            message = msg.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "text":
                    content_parts.append(block.get("text", ""))
        elif msg.get("type") == "tool_call" and msg.get("subtype") == "started":
            tool_call_data = msg.get("tool_call", {})
            for tool_type in TOOL_CALL_TYPES:
                if tool_type in tool_call_data:
                    tools_used.append({
                        "name": tool_type.replace("ToolCall", ""),
                        "timestamp": msg.get("timestamp_ms"),
                    })
                    break
    
    return ClaudeResponse(
        content=result.get("result", "") or "\n".join(content_parts),
        session_id=result.get("session_id", ""),
        cost=0.0,  # cursor-agent doesn't provide cost
        duration_ms=result.get("duration_ms", 0),
        num_turns=len([m for m in messages if m.get("type") == "assistant"]),
        is_error=result.get("is_error", False),
        error_type=result.get("subtype") if result.get("is_error") else None,
        tools_used=tools_used,
    )
```

---

## 5. Приоритеты

| Приоритет | Задача | Сложность | Время |
|-----------|--------|-----------|-------|
| 🔴 High | Фаза 1: CursorAgentManager | Medium | 2-3 часа |
| 🔴 High | Фаза 2: Конфигурация | Low | 30 мин |
| 🟡 Medium | Фаза 3: Интеграция в фасад (без fallback) | Low | 1 час |
| 🟡 Medium | Фаза 4: Парсер сообщений | Medium | 1-2 часа |
| 🔴 High | Фаза 5: Кнопки `.claude/commands` | Medium | 2 часа |
| 🟢 Low | Фаза 6: Тестирование | Medium | 2-3 часа |
| 🟢 Low | Фаза 7: Документация | Low | 30 мин |

**Итого:** ~10-13 часов

---

## 6. Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| cursor-agent не установлен | Medium | Medium | Понятное сообщение об ошибке с инструкцией по установке |
| Изменение формата вывода | Low | High | Версионирование парсера, логирование неизвестных типов |
| Отсутствие cost метрики | High | Low | cursor-agent не возвращает cost — оставляем 0.0 |
| Различия в tool naming | Medium | Medium | Создать маппинг cursor-agent tools → Claude tools |
| Отсутствие аутентификации | Low | High | Проверка `cursor-agent status` при инициализации |
| Пустая директория `.claude/commands` | High | Low | Показать сообщение "No commands found" |
| Некорректный формат .md файлов | Medium | Low | Graceful handling, пропуск файлов без заголовка |

> **Примечание:** Fallback НЕ используется. Если выбранный агент недоступен — возвращаем ошибку.

---

## 7. Зависимости

### Внешние
- `cursor-agent` CLI должен быть установлен и авторизован
- Node.js runtime для cursor-agent

### Проверка готовности
```bash
# Проверить установку
cursor-agent --version

# Проверить авторизацию
cursor-agent status
```

---

## 8. Сравнение режимов интеграции

| Характеристика | Claude SDK | Claude CLI | cursor-agent |
|----------------|------------|------------|--------------|
| Тип интеграции | Python SDK | subprocess | subprocess |
| Streaming | Да | Да | Да |
| Thinking output | Нет | Нет | Да |
| Cost tracking | Да | Да | Нет |
| Session resume | Да | Да | Да |
| MCP support | Частичный | Частичный | Полный |
| Tool calls format | ToolUseBlock | JSON stream | Nested JSON |
| `.claude/commands` | Нет | Нет | Да |

### Выбор режима (через конфигурацию)

```bash
# Использовать cursor-agent
USE_CURSOR_AGENT=true

# Использовать Claude SDK (Python)
USE_SDK=true
USE_CURSOR_AGENT=false

# Использовать Claude CLI (subprocess)
USE_SDK=false
USE_CURSOR_AGENT=false
```

> **Важно:** Выбирается ОДИН режим. Fallback отсутствует. При ошибке — понятное сообщение пользователю.

---

## 9. Чеклист готовности к production

### cursor-agent интеграция
- [ ] CursorAgentManager полностью реализован
- [ ] Парсинг всех типов сообщений работает
- [ ] Конфигурация через .env работает
- [ ] Логирование охватывает все ключевые операции
- [ ] Telemetry spans добавлены
- [ ] Понятные сообщения об ошибках при недоступности агента

### Project Commands (`.claude/commands`)
- [ ] Сканирование директории работает
- [ ] Кнопки отображаются корректно
- [ ] Callback handler обрабатывает нажатия
- [ ] Команда `/commands` зарегистрирована
- [ ] Выполнение команд через AI агента работает

### Общее
- [ ] Все unit-тесты проходят
- [ ] Документация обновлена (CLAUDE.md)
- [ ] .env.example обновлён
- [ ] Проведено ручное тестирование
- [ ] Нет fallback логики — только explicit конфигурация

---

## 10. Детали `.claude/commands`

### Что это такое

Директория `.claude/commands/` в проекте содержит markdown-файлы с предопределёнными командами/промптами для AI агента. Это позволяет:
- Создавать повторно используемые команды для проекта
- Стандартизировать операции (релизы, код-ревью, etc.)
- Делиться командами между членами команды через git

### Структура файла команды

```markdown
# Command Title

Description of what this command does.

## Instructions

Step-by-step instructions for the AI agent...

## Example

Example usage and expected output...
```

### Примеры команд

| Команда | Описание |
|---------|----------|
| `release-upcoming` | Показать изменения для следующего релиза |
| `release-build` | Собрать релиз |
| `release-ticket` | Создать тикет для релиза |
| `code-review` | Провести код-ревью изменений |
| `test-coverage` | Проверить покрытие тестами |

### Как работает

1. Пользователь отправляет `/commands` или переходит в проект
2. Бот сканирует `.claude/commands/*.md`
3. Показывает inline-кнопки с доступными командами
4. При нажатии кнопки:
   - Читает содержимое `.md` файла
   - Отправляет как промпт в AI агент
   - Стримит ответ пользователю

### Безопасность

- Команды читаются только из `APPROVED_DIRECTORY`
- Путь к файлу валидируется (защита от path traversal)
- Файлы читаются только с расширением `.md`
