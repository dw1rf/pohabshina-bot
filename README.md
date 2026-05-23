# Pohabshina Bot

Discord-бот на **discord.py 2.x** с системой кино-подборок, модерацией, уровнями, репутацией, reaction roles, технической поддержкой (тикеты) и shop-панелью услуг.

## Что умеет бот

- **Фильмы / сериалы**: подбор и публикация контента в целевые каналы (через Watchmode API).
- **Модерация**: warn/mute/unmute/ban/unban/kick + правила сервера.
- **Уровни**: подсчёт активности и ранги.
- **Репутация**: текстовые `+реп`/`-реп` и `+rep`/`-rep` ответом на сообщение или сразу после сообщения участника.
- **Reaction roles**: сообщения с реакциями для автополучения ролей.
- **Техподдержка и магазин услуг**:
  - `/support_panel` — панель с кнопкой создания приватного тикета.
  - `/shop_panel` — панель магазина с выбором услуги через select.
  - persistent UI (кнопки и select работают после перезапуска бота).

---

## Быстрый запуск

1. Установите зависимости:

```bash
pip install -r requirements.txt
```

2. Создайте `.env` в корне проекта.
3. Заполните переменные окружения (см. блок ниже).
4. Запустите бота:

```bash
python bot.py
```

---

## Docker / BotHost

Музыкальный модуль и TTS используют системные бинарники, а не Python-пакеты:

- `ffmpeg` нужен для `discord.FFmpegPCMAudio`;
- `deno` нужен `yt-dlp` для стабильной работы с YouTube;
- `libopus0` нужен голосовому стеку Discord.

Эти зависимости нельзя поставить через `requirements.txt`. На BotHost нужно запускать проект как Docker image/custom Dockerfile. Текущий `Dockerfile` уже устанавливает `ffmpeg`, `deno` и `libopus0`, а при сборке проверяет, что команды `ffmpeg` и `deno` доступны в `PATH`.

Dockerfile сделан по схеме из инструкций хостинга: `python:3.12-slim-bookworm` + `apt-get install ffmpeg` внутри image. Если проект запущен как обычный Python egg, этот Dockerfile не используется, поэтому `ffmpeg` внутри контейнера не появится.

Для Bothost `agentv3` в `requirements.txt` также добавлен `ffmpeg-python`: проект не использует его API напрямую, но генератор Bothost по этой зависимости автоматически добавляет системный apt-пакет `ffmpeg` в сгенерированный Dockerfile.

Стартовый файл проекта — `bot.py`. В Dockerfile он запускается как `python -u bot.py`; в Pterodactyl startup command должен быть тот же entrypoint, если сервер запускается не через `CMD` из Dockerfile.

Если в логах остаётся ошибка `shutil.which('ffmpeg') returned nothing`, значит хостинг запустил не этот Dockerfile или контейнер не был пересобран. Пересоберите image с нуля и проверьте внутри контейнера:

```bash
which ffmpeg
ffmpeg -version
which ffprobe
ffprobe -version
which deno
deno --version
```

При успешном старте бот пишет в лог:

```text
Voice runtime: PATH=...
ffmpeg found: path=/usr/bin/ffmpeg version=ffmpeg version ...
ffprobe found: path=/usr/bin/ffprobe version=ffprobe version ...
deno found: path=/usr/local/bin/deno version=deno ...
PyNaCl is installed; Discord voice support can load.
```

Если вместо этого есть `FFmpeg не найден в контейнере`, значит запущенный контейнер/egg всё ещё собран без системного apt-пакета `ffmpeg`.

---

## Переменные `.env`

### Обязательные

```env
DISCORD_TOKEN=ваш_discord_bot_token
WATCHMODE_API_KEY=ваш_watchmode_api_key
```

### Основные настройки бота

```env
WATCHMODE_REGION=US
WATCHMODE_LIMIT=80
TARGET_CHANNEL_ID=0
CONTROL_CHANNEL_ID=0
DELETE_CONTROL_MESSAGES=false
ALLOWED_USER_IDS=
ALLOWED_ROLE_IDS=
MOD_LOG_CHANNEL_ID=0
```

### Перевод / отображение

```env
TRANSLATE_API_URL=
TRANSLATE_API_KEY=
TRANSLATE_SOURCE_LANG=auto
TRANSLATE_TARGET_LANG=ru
SHOW_BOTH_TITLES=true
```

### База данных и социальные функции

```env
SQLITE_PATH=data/bot.db
```

### Поддержка и магазин (новое)

```env
SUPPORT_CATEGORY_ID=0
SUPPORT_ADMIN_ROLE_ID=0
SUPPORT_LOG_CHANNEL_ID=0
SHOP_REQUESTS_TO_SUPPORT=true
```

#### Пояснение по support/shop

- `SUPPORT_CATEGORY_ID` — ID категории, где будут создаваться тикеты `ticket-<user_id>`.
- `SUPPORT_ADMIN_ROLE_ID` — роль администраторов поддержки (видят и закрывают тикеты).
- `SUPPORT_LOG_CHANNEL_ID` — канал логов закрытия тикетов (опционально, `0` = выключено).
- `SHOP_REQUESTS_TO_SUPPORT` — включение заявок из shop-панели (`true/false`).

---

## Как работает support/ticket система

1. Администратор отправляет `/support_panel`.
2. Пользователь нажимает кнопку **«Создать обращение»**.
3. Бот создаёт приватный канал в `SUPPORT_CATEGORY_ID` с правами:
   - `@everyone` — не видит канал;
   - автор тикета — читать/писать;
   - роль `SUPPORT_ADMIN_ROLE_ID` — читать/писать/модерировать;
   - бот — полный доступ для работы в канале.
4. Внутри тикета бот отправляет embed «Обращение создано» и кнопку закрытия.
5. При закрытии:
   - канал удаляется;
   - в `SUPPORT_LOG_CHANNEL_ID` (если задан) уходит лог закрытия.

Система защищена от дублей: у пользователя одновременно может быть только один открытый тикет.

---

## Как работает shop-панель

1. Администратор отправляет `/shop_panel`.
2. Пользователь выбирает услугу из select-меню.
3. Бот создаёт тикет (или использует уже открытый) и отправляет embed «Заявка на услугу».
4. Для услуг со статусом «Скоро» заявка тоже создаётся, но помечается как требующая согласования.

---

## AI-NPC сервера

AI-модуль живёт в `cogs/ai_chat.py` и использует персонажный сервис `services/ai_persona_service.py`. Он работает только в каналах, включённых через `/ai channel_enable`, и не должен отвечать на каждое сообщение: ответы ограничены cooldown, дневным лимитом и шансом случайной реплики.

Старые команды сохранены:

- `/ai ask` - публичный вопрос AI.
- `/ai private` - приватный вопрос AI.
- `/ai channel_enable` - включить AI в текущем канале.
- `/ai channel_disable` - выключить AI в текущем канале.

Новые команды:

- `/ai history` - поиск по сохранённой AI-памяти.
- `/ai creepy` - отправить случайную локальную картинку из `assets/creepy`.
- `/ai image_describe` - описать прикреплённую картинку, если включён vision-режим.
- `/ai afisha` - сделать preview афиши и опубликовать только после подтверждения админа.
- `/ai_relation set/show/reset` - вручную управлять отношением AI к пользователям.
- `/ai_config status` - показать provider, persona, лимиты, usage, память, реакции и mood.
- `/ai_config mood_set` - вручную поставить mood сервера.
- `/ai_config memory_clear_user` - очистить память по пользователю.
- `/ai_config memory_clear_channel` - очистить память текущего канала.
- `/ai_config random_replies` - включить или выключить случайные реплики.
- `/ai_config reactions` - включить или выключить реакции.

AI не является модератором: он не должен самовольно мутить, банить, кикать или обещать реальные наказания. Ответы проходят sanitization: удаляются `<think>...</think>`, meta-фразы модели, прямые mentions и реальные угрозы. Запрещённые угрозы заменяются безопасной театральной фразой.

### AI `.env`

```env
AI_PROVIDER=gemini
AI_PERSONA=pohab_npc
AI_RANDOM_REPLY_CHANCE=0.04
AI_GLOBAL_COOLDOWN_SECONDS=60
AI_USER_COOLDOWN_SECONDS=120
AI_DAILY_LIMIT=700
AI_CONTEXT_LIMIT_CHARS=1500
AI_MEMORY_DAYS=30
AI_BOT_ALIASES=мурка,бот,пахабщина,пахаб
AI_ENABLE_MEMORY=true
AI_ENABLE_RANDOM_REPLIES=true
AI_ENABLE_REACTIONS=true
AI_ENABLE_IMAGE_DESCRIBE=false
AI_ALLOW_SUPPORT_TICKETS=false
AI_MAX_PROMPT_LENGTH=1800
AI_MAX_OUTPUT_TOKENS=500
AI_TEMPERATURE=0.75

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b
GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=gemma3
```

### Что делают AI env

| Переменная | По умолчанию | Что делает |
| --- | --- | --- |
| `AI_PROVIDER` | `gemini` | Выбирает backend генерации: `gemini`, `groq` или `ollama`. |
| `AI_PERSONA` | `pohab_npc` | Имя режима персонажа для статуса и конфигурации. Логика prompt сейчас живёт в `AIPersonaService`. |
| `AI_RANDOM_REPLY_CHANCE` | `0.04` | Вероятность редкой самостоятельной реплики на обычное сообщение в AI-канале. `0.04` = примерно 4%, но cooldown всё равно ограничивает ответы. |
| `AI_GLOBAL_COOLDOWN_SECONDS` | `60` | Минимальная пауза между AI-ответами на всём сервере. Главная защита от сжигания Groq Free. |
| `AI_USER_COOLDOWN_SECONDS` | `120` | Минимальная пауза между AI-ответами одному пользователю. |
| `AI_DAILY_LIMIT` | `700` | Максимум AI-запросов в сутки на сервер. Хранится в SQLite в `ai_usage_daily`. |
| `AI_CONTEXT_LIMIT_CHARS` | `1500` | Максимальный размер локального контекста, который добавляется к prompt: память канала, память пользователя, relation и mood. |
| `AI_MEMORY_DAYS` | `30` | Сколько дней хранить AI-память сообщений. Старые записи чистятся при запуске AI cog. |
| `AI_BOT_ALIASES` | `мурка,бот,пахабщина,пахаб` | Слова, на которые бот реагирует как на обращение к себе без прямого Discord mention. Разделитель - запятая. |
| `AI_ENABLE_MEMORY` | `true` | Включает сохранение очищенных сообщений в AI-enabled каналах. Не сохраняет ботов, команды, слишком короткий текст, invite/email/телефоны/tokens в открытом виде. |
| `AI_ENABLE_RANDOM_REPLIES` | `true` | Глобальный env-дефолт для редких случайных ответов. На сервере можно переопределить через `/ai_config random_replies`. |
| `AI_ENABLE_REACTIONS` | `true` | Глобальный env-дефолт для локальных реакций без AI-запроса. На сервере можно переопределить через `/ai_config reactions`. |
| `AI_ENABLE_IMAGE_DESCRIBE` | `false` | Включает `/ai image_describe`. По умолчанию выключено, потому что vision-запросы дороже и требуют Groq ключ. |
| `AI_ALLOW_SUPPORT_TICKETS` | `false` | Разрешает AI отвечать в support/ticket каналах. По умолчанию выключено, чтобы AI не мешал поддержке. |
| `AI_MAX_PROMPT_LENGTH` | `1800` | Максимальная длина пользовательского prompt для slash-команд и автоответов. |
| `AI_MAX_OUTPUT_TOKENS` | `500` | Лимит генерации ответа у AI provider. |
| `AI_TEMPERATURE` | `0.75` | Температура генерации. Выше = более хаотичный стиль, ниже = суше и стабильнее. |
| `GEMINI_API_KEY` | пусто | Ключ Gemini, если `AI_PROVIDER=gemini`. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Модель Gemini. |
| `GROQ_API_KEY` | пусто | Ключ Groq, если `AI_PROVIDER=groq` или используется `/ai image_describe`. |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Текстовая модель Groq. |
| `GROQ_VISION_MODEL` | `meta-llama/llama-4-scout-17b-16e-instruct` | Vision-модель Groq для `/ai image_describe`. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | URL локального Ollama API, если `AI_PROVIDER=ollama`. |
| `OLLAMA_MODEL` | `gemma3` | Локальная модель Ollama. |

### Рекомендации для Groq Free

Для бесплатного Groq лучше начинать с консервативных значений:

```env
AI_RANDOM_REPLY_CHANCE=0.02
AI_GLOBAL_COOLDOWN_SECONDS=90
AI_USER_COOLDOWN_SECONDS=180
AI_DAILY_LIMIT=300
AI_MAX_OUTPUT_TOKENS=350
```

Если бот слишком молчаливый, сначала поднимайте `AI_RANDOM_REPLY_CHANCE` до `0.04-0.05`, а не уменьшайте cooldown. Если бот упирается в 429, увеличивайте `AI_GLOBAL_COOLDOWN_SECONDS` и снижайте `AI_DAILY_LIMIT`.

### AI-память и приватность

Память сохраняется только в каналах, где включён AI. Перед записью текст чистится:

- Discord invite links заменяются на `[invite]`;
- email заменяются на `[email]`;
- телефоны заменяются на `[phone]`;
- длинные числа заменяются на `[number]`;
- token-like строки заменяются на `[token]`;
- markdown и лишние пробелы удаляются;
- запись обрезается до 500 символов.

Таблицы создаются автоматически:

- `ai_message_memory` - очищенная память сообщений;
- `ai_user_relations` - relation, affection, irritation, nickname;
- `ai_guild_mood` - mood, chaos, irritation;
- `ai_usage_daily` - дневной счётчик AI-запросов;
- `ai_guild_settings` - серверные overrides для random replies и reactions.

### Отношения и mood

Relation может быть:

- `favorite` - бот мягче и теплее подкалывает пользователя;
- `neutral` - обычный режим;
- `rival` - более колючий и язвительный стиль;
- `ignored` - почти всегда молчит;
- `cursed` - мрачный и криповый стиль.

Mood сервера может быть:

- `neutral`;
- `playful`;
- `annoyed`;
- `creepy`;
- `sleepy`;
- `friday_chaos`;
- `horny_chaos`.

Mood считается локально без AI-запроса: вопросы, смех, капс, мат, грустные и хаотичные сообщения меняют состояние сервера. Ночью mood может уходить в `sleepy`/`creepy`, в пятницу - в `friday_chaos`.

---

## Примечания

- Бот использует SQLite и автоматически создаёт нужные таблицы при запуске.
- Если support-категория/роль не настроены, бот покажет администратору понятную ошибку.
- Для корректной работы команд и каналов проверьте права бота на сервере.
