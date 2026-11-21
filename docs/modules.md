# Модули Discord-бота

В приложении 32 загружаемых расширения из `cogs/`. Список составлен по результату загрузки расширений и регистрации команд с подменой сетевой синхронизации Discord. Команды приведены по именам из кода. Клиент Discord может показывать русские локализованные названия.

Для оценки спроса выберите период, например последние 30 дней, и считайте действия и разных участников своего сервера. Для автоматических публикаций проверьте доставки и спросите читателей. Число вызовов админской команды настройки мало говорит о том, читают ли ленту или нажимают ли кнопки. Для slash- и префиксных команд используйте админскую `/command_stats`. Учёт начинается после обновления.

## Модерация, поддержка и управление сервером

| Модуль | Что делает | Где посмотреть использование или зависимость |
| --- | --- | --- |
| `command_stats` | Админская статистика команд, кнопок, реакций и репутации. | `view:modules` показывает охват и действия по модулям, `view:inactive` — модули без учтённых действий. |
| `moderation` | Предупреждения, тайм-ауты, бан, кик, тюрьма и публикация правил. Бот сам снимает тюремную роль по расписанию. | Журнал модерации, таблицы `warnings` и `active_jails`. Перед отключением проверить действующие наказания. |
| `automod` | Проверяет сообщения и вход новых участников, применяет правила спама и защиты от рейдов. | Настройки и `automod_incidents`. `levels` и `social_profile` используют результат проверки для учёта активности. |
| `ping_guard` | Реагирует на повторные упоминания отсутствующего участника, может послать уведомление в ЛС. | `smart_notifications` и `ping_events`. Работает при сообщениях. |
| `reaction_ban` | Запрещает выбранному участнику ставить реакции и обрабатывает нарушение. | Действующие записи `reaction_bans` и срабатывания в каналах. |
| `reaction_roles` | Выдаёт и снимает роли по реакциям на настроенные сообщения. | Панели и `reaction_role_bindings`. Участники пользуются реакциями, команды обычно вызывает администратор. |
| `quick_help` | Отвечает на фразы о помощи, правилах и ролях. Показывает кнопки магазина и поддержки. | Ответы бота на «где правила», «как получить роль» и «помогите», переходы с панели. |
| `welcome` | Публикует приветствие при входе участника. | `welcome_settings` и последние приветствия в настроенном канале. |
| `relay` | Публикует текст, карточки и афиши от имени бота. | Сообщения и афиши бота в каналах, которыми пользуются модераторы. |
| `settings` | Настройки социальных функций, аналитики, знакомств, логов и каналов 18+. | `guild_settings`. Отключение убирает команды изменения этих настроек. |

## Общение и публикации

| Модуль | Что делает | Где посмотреть использование или зависимость |
| --- | --- | --- |
| `ai_chat` | AI-чат, вопросы, память, поиск, отношения с персонажем, афиши и описание изображений. | `ai_usage_daily` по дням и включённые `ai_channels`. Есть автоматические ответы. |
| `birthdays` | Календарь дней рождения и автоматические поздравления. | `birthdays` и даты последних поздравлений. |
| `suggestions` | Предложения участников, голосование кнопками и решения администрации. | `community_suggestions` и `community_suggestion_votes`. |
| `giveaways` | Розыгрыши с кнопкой участия, выбором победителей и автозавершением. | `giveaways` и `giveaway_entries`. Перед отключением проверить текущие розыгрыши. |
| `starboard` | Переносит отмеченные звёздами сообщения в подборку сервера. | `starboard_posts` и свежие публикации в канале подборки. |
| `weekly_digest` | Готовит и публикует отчёт о недельной активности. | `community_digest_deliveries` и просмотры публикаций, если у сервера есть такая статистика. |
| `nsfw_import` | Копирует сообщения из настроенного канала 18+ в целевой канал 18+. Настраивается через `settings`. | `nsfw_import_deliveries` и публикации в канале назначения. |

## Музыка, озвучка и кино

| Модуль | Что делает | Где посмотреть использование или зависимость |
| --- | --- | --- |
| `music` | Голосовой плеер, поиск музыки, плейлисты, очередь и панель управления. | Сеансы воспроизведения в логах, текстовые команды и кнопки. Очереди хранятся в памяти. |
| `tts_voice` | Озвучивает фразу или читает сообщения пользователя в голосовом канале. | Фактические сеансы озвучки. Делит голосовое подключение с `music`. |
| `movie_rating` | Голосование за просмотренный фильм, подсчёт итогов и закрытие сессий. | `movie_rating_sessions` и `movie_rating_votes`. Участники голосуют кнопками. |

## Игры, профили и социальные функции

| Модуль | Что делает | Где посмотреть использование или зависимость |
| --- | --- | --- |
| `levels` | Считает подходящие сообщения, повышает уровень, выдаёт награду и показывает ранг. | `levels` и сообщения о повышениях. Отключение затронет `/top levels`. |
| `reputation` | Участники меняют репутацию сообщениями `+реп`, `-реп`, `+rep`, `-rep`. Есть админская коррекция. | Свежие `reputation_events` и число разных участников. |
| `social_profile` | Собирает разрешённую аналитику сообщений, показывает профили и предоставляет управление данными. | Открытие профилей и `user_privacy_settings`. Фоновое накопление само по себе не показывает спрос на просмотр профиля. |
| `economy` | Кошелёк, банк, заработок, инвентарь, игровые покупки, рынок, задания и события. | `economy_transactions` по причинам операций. Отделять ручные операции от автоматических начислений. |
| `progression` | Достижения, сезоны, коллекции, оформление профиля, рейды питомцев, войны клубов и доставка наград. | `season_reward_claims`, `guild_quest_claims` и `pet_boss_contributions`. Сервис прогресса также вызывают другие модули. |
| `pets` | Уход за питомцами, приключения и бои. | `pet_actions`, `pet_battles` и участие разных владельцев. |
| `club` | Управление игровыми клубами, персоналом, доходом, участниками и общим банком. | `club_transactions`, `club_applications` и `club_members`. |
| `weddings` | Предложения, браки, подарки, совместные задания и развитие отношений. | Отдельная `data/weddings.db`, в том числе `wedding_proposals` и `relationship_gift_log`. |
| `story` | Интерактивная история со сценами, выборами и сохранением прогресса. | `story_progress` и переходы по сценам через кнопки. |
| `roleplay` | Ролевые действия с участниками, включая отдельные команды, регистрируемые для сервера. | `rp_action_counters` и опубликованные действия. Для действий 18+ действует ограничение канала. |
| `fun` | Короткие шуточные команды взаимодействия с участниками. | Ответы бота в чате или данные об использовании команд. |
| `top` | Выводит таблицы лидеров по разным направлениям. | Вызовы `/top`. Для уровней, экономики, питомцев, клубов и пар обращается к соответствующим модулям. |

## Справочник команд

Глобальные команды, команды отдельных серверов и текстовые псевдонимы собраны из дерева `discord.py`. Команды `!…` относятся к префиксному интерфейсу. У библиотеки также есть стандартная `!help`.

### command_stats

`/command_stats`. Для оценки востребованности сравните число участников и типы действий за 30–90 дней. Кнопки и меню, реакции Starboard и выдачи ролей, а также успешные `+реп` и `-реп` учитываются с момента установки этой версии. При оценке автоматических модулей дополнительно проверьте их доставки и срабатывания.

### moderation

`/ban`, `/clearwarns`, `/jail`, `/kick`, `/mute`, `/publish_rules`, `/unban`, `/unjail`, `/unmute`, `/warn`, `/warnings`.

### automod

`/automod exempt`, `/automod off`, `/automod raid_off`, `/automod raid_status`, `/automod rule`, `/automod setup`, `/automod status`.

Обрабатывает `on_member_join`, `on_message`.

### ping_guard

Модуль реагирует на события Discord.

Обрабатывает `on_message`.

### reaction_ban

`/reaction_ban`, `/reaction_ban_list`, `/reaction_unban`.

Обрабатывает `on_reaction_add`.

### reaction_roles

`/reactionroles add`, `/reactionroles create`, `/reactionroles delete`, `/reactionroles list`, `/reactionroles remove`.

Обрабатывает `on_raw_reaction_add`, `on_raw_reaction_remove`.

### quick_help

`/helpme`, `/how`, `/role`, `/rules`.

Обрабатывает `on_message`.

### welcome

`/welcome off`, `/welcome preview`, `/welcome set`, `/welcome status`.

Обрабатывает `on_member_join`.

### relay

`/say`, `/say_afisha`, `/say_embed`.

### settings

`/bot_settings`, `/set_log_channel`, `/set_matchmaking`, `/set_nsfw_channel`, `/set_nsfw_import_channel`, `/set_profile_analytics`, `/set_story_nsfw`.

### ai_chat

`/ai afisha`, `/ai ask`, `/ai channel_disable`, `/ai channel_enable`, `/ai creepy`, `/ai history`, `/ai image_describe`, `/ai private`, `/ai_config memory_clear_channel`, `/ai_config memory_clear_user`, `/ai_config mood_set`, `/ai_config random_replies`, `/ai_config reactions`, `/ai_config status`, `/ai_relation reset`, `/ai_relation set`, `/ai_relation show`.

Обрабатывает `on_message`.

### birthdays

`/др посмотреть`, `/др список`, `/др удалить`, `/др установить`.

Фоновые задачи — `birthday_check_loop`.

### suggestions

`/suggestion create`, `/suggestion decide`, `/suggestion off`, `/suggestion setup`, `/suggestion status`.

Фоновые задачи — `projection_loop`.

### giveaways

`/giveaway create`, `/giveaway end`, `/giveaway reroll`.

Фоновые задачи — `finish_loop`.

### starboard

`/starboard exclude`, `/starboard off`, `/starboard set`, `/starboard status`.

Обрабатывает `on_raw_reaction_add`, `on_raw_reaction_remove`.

### weekly_digest

`/digest off`, `/digest preview`, `/digest setup`, `/digest status`.

Фоновые задачи — `delivery_loop`.

### nsfw_import

Модуль реагирует на события Discord.

Обрабатывает `on_message`.

### music

`/music autoplay`, `/music clear`, `/music current`, `/music leave`, `/music next`, `/music now`, `/music panel`, `/music pause`, `/music play`, `/music queue`, `/music resume`, `/music shuffle`, `/music skip`, `/music stop`.

Текстовые команды и их псевдонимы

`!autoplay`, `!автоплей` · `!clear`, `!очистить` · `!current`, `!nowplaying`, `!сейчас_играет`, `!трек` · `!leave`, `!выйти` · `!pause`, `!пауза` · `!play`, `!играть` · `!playnext`, `!next`, `!следующим` · `!playnow`, `!now`, `!сейчас` · `!queue`, `!очередь` · `!resume`, `!продолжить` · `!shuffle`, `!перемешать` · `!skip`, `!пропустить` · `!stop`, `!стоп`.

Обрабатывает `on_voice_state_update`.

### tts_voice

`/tts`, `/tts_say`.

Обрабатывает `on_message`, `on_voice_state_update`.

### movie_rating

`/rating close`, `/rating list`, `/rating post`, `/rating results`.

Фоновые задачи — `close_due_sessions`.

### levels

`/rank`.

Обрабатывает `on_message`.

### reputation

`/reputation_admin change`.

Обрабатывает `on_message`.

### social_profile

`/forget_me`, `/privacy`, `/profile`, `/profile_user`.

Обрабатывает `on_message`.

### economy

`/bank deposit`, `/bank withdraw`, `/economy cooldowns`, `/economy crime`, `/economy daily`, `/economy fish`, `/economy mine`, `/economy profile`, `/economy transfer`, `/economy work`, `/economy_admin event_start`, `/economy_admin grant`, `/economy_admin item`, `/economy_admin ledger`, `/economy_admin status`, `/event collect`, `/event leaderboard`, `/event view`, `/inventory gift`, `/inventory use`, `/inventory view`, `/market buy`, `/market cancel`, `/market list`, `/market search`, `/quest claim`, `/quest list`, `/shop browse`, `/shop buy`, `/shop sell`.

### progression

`/achievements`, `/club_war`, `/club_war_claim`, `/collection`, `/customize equip`, `/customize list`, `/customize preview`, `/features status`, `/pet_equip`, `/pet_equipment`, `/pet_evolve`, `/pet_raid`, `/progress_admin club_war_start`, `/progress_admin quest_start`, `/progress_admin season_start`, `/season claim`, `/season view`, `/serverquest claim`, `/serverquest view`.

Фоновые задачи — `reward_delivery_loop`.

### pets

`/pet`, `/pet_adventure`, `/pet_battle`.

### club

`/club`, `/community accept`, `/community apply`, `/community deposit`, `/community leave`, `/community profile`, `/community role`, `/community skill_upgrade`, `/community skills`, `/community store`, `/community take`, `/community withdraw`.

### weddings

`/wedding collection`, `/wedding divorce`, `/wedding gift`, `/wedding marry`, `/wedding memories`, `/wedding profile`, `/wedding quest`, `/wedding relationships`, `/wedding stats`.

### story

`/story`.

### roleplay

`/rp act`, `/rp stats`.

Дополнительные команды сервера

`/cuff_bed`, `/взять_за_волосы`, `/водить_членом_по_губам`, `/вставить_кляп`, `/встать_на_колени`, `/встать_раком_перед_кем_то`, `/завязать_глаза`, `/задрать_юбку`, `/зажать_рот`, `/заставить_встать_на_колени`, `/заставить_встать_раком`, `/засунуть_пальцы_в_рот`, `/заткнуть`, `/кончить_на_лицо`, `/лизать_подошву`, `/лизнуть_шею`, `/массировать_грудь`, `/надеть_кляп`, `/надеть_ошейник`, `/наступить_на_горло`, `/облизать_обувь`, `/опуститься_на_четвереньки`, `/плюнуть_слюной_в_рот`, `/погладить`, `/потянуть_за_поводок`, `/прижать`, `/прижать_к_стенке`, `/прижать_лбом_к_полу`, `/прижечь_воском`, `/пристегнуть_цепь`, `/раздвинуть_ноги_рукой`, `/связать_запястья`, `/сесть_на_лицо`, `/сесть_у_ног`, `/снять_бельё_зубами`, `/стукнуть_членом_по_щеке`, `/ударить_ладонью`, `/ударить_лицом_об_пол`, `/ударить_плетью`, `/ущипнуть`, `/французский_поцелуй`, `/шлепнуть`, `/щекотать`.

Обрабатывает `on_guild_join`, `on_ready`.

### fun

`/кастрировать`, `/легенда`, `/минет`, `/надуть`, `/обнять`, `/похвалить`, `/поцеловать_ступню`, `/пригласить_на_чай`, `/самый_красивый`, `/убить`, `/шлёпнуть`.

### top

`/top clubs`, `/top economy`, `/top events`, `/top levels`, `/top messages`, `/top pairs`, `/top pets`, `/top relations`, `/top reputation`.

## Общий код

`bot.py` запускает приложение, `config.py` читает конфигурацию, `bot_client.py` открывает соединения с SQLite, загружает расширения, синхронизирует команды и устанавливает игровую активность BlockEra Launcher. Кнопка лаунчера доступна в `/helpme` и `/how`.

В каталоге `cogs/` также находятся два вспомогательных модуля. `music_resolvers.py` разбирает ссылки музыкальных сервисов для `music`. `social_game_content.py` содержит данные для `pets`, `roleplay` и `story`.

В `services/` находятся `ai_persona_service`, `automod_service`, `community_ops_service`, `economy_service`, `jail_service`, `level_service`, `progression_service`, `reaction_ban_service`, `reaction_role_service`, `reputation_service`, `social_game_service`.

В `utils/` находятся `bot_profile`, `brand`, `command_localizations`, `embed_format`, `embeds`, `engagement_content`, `helpers`, `leaderboard_image`, `message_commands`, `module_activity`, `permissions`, `sqlite_writes`, `static_data`, `tts_text_filter`, `voice_runtime`. Файлы `__init__.py` обозначают пакеты.

Планируя отключение, учитывайте существующие кнопки розыгрышей, сроки наказаний и ожидающие награды. Загрузчик расширений и инициализация общих сервисов находятся в разных частях `bot_client.py`. Удаление файла расширения само по себе не останавливает запись в общие таблицы другими модулями.
