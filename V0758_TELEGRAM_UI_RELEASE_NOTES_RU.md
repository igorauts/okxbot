# v0.7.58 — Telegram Financial UI maintenance — 5 ноября 2026

**RELEASE_VERSION=0.7.58; ENGINE_VERSION=0.7.57; RELEASE_KIND=maintenance.**
Торговый движок, стратегия, позиции, заявки, Reader и StatsWorker не менялись.
Публикация синхронизирует с GitHub уже установленные Telegram-only изменения.

## Telegram Monitor

- `/status`: компактный read-only финансовый обзор, сортировка по накопленному GRID PROFIT, выравнивание колонки `=`, позиции и BID с корректной точностью. Боты 🟢/🔴 (⚪ если PnL неизвестен), общий TOTAL 🟩/🟥 (⬜ неизвестен).
- `Account Equity`, `Balance`, `Floating PnL`: выделенный GET-only помощник повторно использует штатные `okx_setup` и `okx_account_ui` менеджера. Один OKX `/api/v5/account/balance` GET при пользовательском `/status` с кратковременным кэшем; GET на этапе GitHub-публикации отсутствует. `Balance = Equity − Floating PnL` для той же учётной метрики, при неполных данных `н/д`.
- `/full`: подробный вывод, круглый индикатор на каждом боте, ACCOUNT TOTAL PnL с квадратом в самом низу, после последнего бота. Длинные отчёты могут разбиваться Telegram на несколько сообщений.
- `/latency`: 60 минут, без блочного `<pre>` и панели Copy. Удалена `/latency8h`; динамическое обнаружение работающих grid-сервисов.
- `/help` и slash-меню Telegram обновлены; регистрация команд запрашивается при запуске монитора.
- Telegram HTTPS-transport: GET polling и отправка сообщений с безопасной ротацией соединений без автоматического replay POST.

## Источник истины и безопасность

- Четыре Telegram-файла берутся *только* из уже установленного LIVE и сверяются по зафиксированному SHA256. Любое расхождение прерывает публикацию.
- GitHub parent: v0.7.57, commit `c8264efe8ee64339902263fe601c4a66e50cb36d`, опубликованный ZIP SHA256 `2b066b2fd37ac801cc4a3f3b98316583a36b4add11ca157fcedc4571b65bdc18`.
- `okx_bot.py` SHA256 `04c2fb1ebddad3c10274e3a81cb1bb944483ee176e0203e40c295bc6230b3400` и `engine_manifest.json` не меняются. `okx_version.py` в GitHub-пакете обновляет только идентификатор release и вид maintenance; двигатель остаётся `0.7.57`. LIVE `okx_version.py` не заменяется.
- Ни GitHub publisher, ни создание ZIP не запускают тестов; ни одного OKX API-запроса, изменения LIVE-файлов или рестарта сервисов.
- Функциональная корректность Telegram-команд после последнего LIVE-патча подтверждается отдельно пользователем; проверка хэшей LIVE-кода не заменяет functional smoke в Telegram.
