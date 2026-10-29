## v0.7.39 LIVE status (29.10.2026 UTC)
7/7 торговых ботов LIVE на новом engine; Priority FIFO ON, Reader IPC 7/7 подтверждён. P0 виртуального MARKET и аварийного пути исправлен. Подробности — V0739_RELEASE_NOTES_RU.md.

> **v0.7.38 LIVE-verified, 28.10.2026 UTC.** Три действующих движка DOOD/BEAT/INIT на 0.7.38, новые BUY/TP и Telegram MARKET SELL подтверждены на LIVE. CAP завершён PANIC SELL с позицией 0 CAP; BTC/NEAR/WLD продолжают работать на 0.7.35. Редкая резервная сверка 5–5.8 сек, реакция на WS-события и fail-closed BUY gate, UI-пары BEAT/CAP/INIT/BLUR/DOS. См. [V0738_LIVE_RELEASE_NOTES_RU.md](V0738_LIVE_RELEASE_NOTES_RU.md). Публикация не меняет VPS.

> **v0.7.36 LIVE-verified, 28.10.2026 UTC.** Четыре действующих бота (NEAR/WLD/BTC/DOOD) остались на торговом движке 0.7.35 и сохранили PID. Общий Reader Diagnostics, регистрация новых ботов, Telegram BYPASS и P2-исправления установлены с backup и проверены без остановки торговых движков. См. [V0736_LIVE_RELEASE_NOTES_RU.md](V0736_LIVE_RELEASE_NOTES_RU.md). Исходная офлайн-записка сохранена в V0736_OFFLINE_RELEASE_NOTES_RU.md.

> **v0.7.35 LIVE-verified, 27.10.2026 UTC.** Три действующих торговых бота NEAR/WLD/BTC обновлены по одному с резервными копиями, GET-only аудитами, свежими heartbeat. Shared Reader использует существующий общий account env; watchdog timer и Telegram Reader FLAPPING/anti-spam LIVE-verified. ZEC и DOOD терминальны, не перезапускались. Исходная офлайн-заметка сохранена: [V0735_OFFLINE_RELEASE_NOTES_RU.md](V0735_OFFLINE_RELEASE_NOTES_RU.md); подтверждения: [V0735_LIVE_RELEASE_NOTES_RU.md](V0735_LIVE_RELEASE_NOTES_RU.md).**

> **v0.7.34 LIVE-verified, 27.10.2026.** При релизном LIVE-аудите все четыре бота работали через Shared Position Reader,
> Telegram Reader watchdog включён. Priority FIFO **не включён**, используется mixed-fifo.
> Исходники и проверки: `V0734_LIVE_RELEASE_NOTES_RU.md`. Публикация GitHub не меняет LIVE.

# OKX Grid Bot

Алгоритмический LONG Grid Bot для бессрочных линейных USDT-SWAP OKX.

**Последний LIVE-verified engine: 0.7.38 (срез 28 октября 2026).** Исторически три бота продолжают работать на engine 0.7.35, а DOOD/BEAT/INIT на 0.7.38; CAP завершён. Подробности описаны в LIVE-заметках. Исторический контекст:  На момент rollout v0.7.34 проверены 4/4 бота. После релиза архивный `zec0724` завершён PANIC SELL (позиция 0); `dood0721-2` тоже терминальный. На момент подготовки v0.7.36 также LIVE запущен новый `dood0735` (не путать с архивным `dood0721-2`); остальные: `near0721`, `wld0728`, `btc0721`. Их свежий статус перед LIVE проверяется отдельно, а не выводится из README.

**DOOD `dood0721-2` завершён PANIC SELL**, позиция 0 DOOD, финальный торговый PnL подтверждён исполнениями OKX. В архиве; не включать его в обычное обновление.

Изменения после опубликованной `0.7.30`: TP-телеметрия, финансовые снимки, attached TP `51006`, ускоренный StatsWorker, исправление Telegram STARTED после PANIC, немедленный WS wake для виртуальных TP и общий Reader для GET позиций. См. [CHANGELOG](CHANGELOG.md), [StatsWorker](STATS_WORKER_OPTIMIZATION_RU.md), [TP Recovery](V0732_TP_RECOVERY_RU.md).

**GitHub-публикация не устанавливает код на VPS, не останавливает торговые службы и не отправляет ордера.**

## v0.7.30 — release highlights

- Устранены привязки к номерам релизов в рабочих функциях и правилах совместимости; сохранены только исторические протокольные имена в persistent файлах.
- `okx_live_upgrade.py`: динамический SHA256-проверяемый обновлятор, backup, stop guards, GET-only audit и обязательное полное подтверждение финансового отчёта до START.
- Telegram сохраняет старый state/outbox/дедупликацию; статистика читает `performance` и legacy `performance_v0724`.
- Реестр движков и observers/PANIC используют проверку идентичности исходного файла и state schema.
- ZIP mode / financial audit completion включены в постоянную регрессию сборки.

**v0.7.28 — Monitoring Reliability Fix.** Продолжение v0.7.27: безопасная группировка heartbeat-инцидентов, append-only 24-часовая latency-телеметрия и однозначные поля TP-диагностики. Контролируемый LIVE rollout и проверка всех 13 служб завершены 23 октября 2026 года. Это результат проверки на момент обновления, не гарантия непрерывной будущей доступности.

## v0.7.28 — Monitoring Reliability Fix (LIVE-verified, 2026-10-23)

- Heartbeat alerts retain each bot's unresolved fault during multi-bot incidents; join and partial recovery are reported without false `ALL RECOVERED`.
- 24-hour latency rolls up in append-only UTC-hour files under `logs/latency_24h_rollups.segments/`; the previous JSONL is read only during a one-day transition. No per-minute rewriting of the full 24h file. Histograms remain approximate; no seven-day archive.
- TP diagnostics include `buyPx`, `tpPx`, `cycleId`, while retaining old `price` for backwards-compatible log readers. Trading choices, FIFO caps, timeouts, TP policy and accounting are unchanged.
- Контролируемое LIVE-обновление четырёх ботов (`near0721`, `btc0721`, `dood0721-2`, `zec0724`) завершено 23 октября 2026 года: отдельные backup, GET-only audit, свежий heartbeat для каждого; 13 служб активны, ошибок в предоставленной проверке журналов нет. Append-only сегменты всех четырёх ботов содержат свежие минутные записи. Групповые аварии на LIVE намеренно не провоцировались; их сценарии пройдены в офлайн-тестах. Публикация GitHub не трогает LIVE.

## Возможности

- Несколько независимых торговых экземпляров на разных инструментах одного OKX-аккаунта.
- Сеточные BUY LIMIT, SELL Take Profit и виртуальные TP.
- Управление через терминальный менеджер `okxbot`.
- Режимы RUN, PAUSE, SOFT STOP, HARD STOP и PANIC SELL.
- Учёт исполнений, торговых циклов, комиссий и PnL.
- Мониторинг максимальной просадки отдельного бота.
- Telegram-монитор: `/status`, `/latency`, `/latency8h`, `/help`.
- Диагностика задержек, аварийных ситуаций и состояния ордеров.

## Основные компоненты

- `okx_bot.py` — торговый движок.
- `okxbot.py` — терминальный менеджер.
- `okx_monitor.py` — мониторинг.
- `okx_execution_observer.py` — наблюдение за исполнением.
- `engine_manifest.json` — версия торгового движка и зависимости.
- `okx_panic_all.py` — независимая проверка доступных экземпляров для частичного PANIC ALL.
- `okx_live_upgrade.py` — контролируемое обновление по одному боту (GET-only audit, backup, fail-closed).
- `okx_*.py` — дополнительные компоненты системы.

## Безопасность

Бот работает с реальными ордерами и позициями. HARD STOP может оставить позицию без защитных TP. PANIC SELL может привести к проскальзыванию и taker-комиссии.

Не размещайте в GitHub API-ключи, `.env`, торговые состояния, SQLite или журналы с чувствительной информацией.

Обновление работающего торгового экземпляра требует отдельной согласованной процедуры с резервным копированием и проверкой состояния. Прямая замена Python-файлов недопустима. Перед использованием `okx_live_upgrade.py` обязательны отдельное разрешение LIVE, явный SHA256 архива, бэкап и понимание риска работающих на OKX ордеров во время технического STOP. Установщик разделяет действия `preflight`, `shared` (менеджер), `telegram` (только Telegram-модули и сервис) и `bot` (по одному торговому боту, требует `--confirm-live-stop`).

## Документация и история

- [Последний релиз](https://github.com/igorauts/okxbot/releases/latest)
- [Все релизы](https://github.com/igorauts/okxbot/releases)
- [История изменений](CHANGELOG.md)

Исходники и документация прежних версий доступны в соответствующих Git-тегах.

## Разработка

Проект развивается на основе действующей LIVE-системы. Перед публикацией новой версии проверяются зависимости, безопасность торговых операций и соответствие исходников установленному коду.

## Изменения v0.7.27 (LIVE проверено 2026-10-23)

- Ускоренная диагностика долгих проходов, heartbeat и общих инцидентов нескольких ботов; REST-таймауты и торговые решения не меняются.
- Исправлено сохранение подтверждённых BUY после пачки; PLACING остаётся отдельным неопределённым intent, не доказательством активного биржевого ордера. Локальные сравнения State/Engine проверяют свежесть снимка.
- BUY→TP метрики только при точном совпадении ordId, с указанием ограничений локальных timestamps. Общие REST/FIFO метрики по маршрутам.
- Пункт Latency Diagnostics: агрегаты за последние 24 часа (не 7 дней), включая P50/P95/P99/MAX; процентили по минутным гистограммам приближённые. Запись быстрых lock/quota событий уменьшена, выбросы сохраняются.
- Персистентные суточные счётчики observer, меньше дублирования и реже обычные burst/BUY_CANCEL_PROTECTED события.
- Read-only сверка PnL SQLite-журнала и снимка статистики; она не исправляет балансы и предупреждает об асинхронных снимках.
- `okx_live_upgrade.py` для перехода 0.7.26→0.7.27 требует отдельного разрешения перед остановкой любого LIVE-бота. Установка уже прошла на near0721, btc0721, dood0721-2, zec0724 с отдельными backup, GET-only audit и новыми PID. GitHub publication не затрагивает LIVE.

## Изменения v0.7.26

- Telegram не отправляет неоперационные `STATE MISMATCH` / `BACK TO NORMAL` по несовпадающим асинхронным State/Engine BUY, но локальный Execution Observer и журналы продолжают работать; все критические тревоги остаются.
- Терминальная статистика: компактный статус RUN и uptime, процент BID к верхнему исполненному BUY, real+virtual TP вместе, количественная точность по спецификации контракта, USD-номиналы с точностью до цента, согласованная цветовая схема.
- Полная офлайн-регрессия и независимая проверка показателей из снимка, без вмешательства в торговую стратегию. Четыре LIVE-бота обновлены по одному с бэкапом и GET-only audit; Telegram monitor активен по результатам rollout. Публикация GitHub не затрагивает LIVE.

## Изменения v0.7.25

- Аварийная отмена отдельно подтверждённых BUY даже при одном неизвестном/изменённом ордере; проблемные ордера не трогаем, PANIC MARKET остаётся блокирован при нерешённом ownership.
- Частичный PANIC ALL для независимо проверенных безопасных экземпляров, с отчётом об отказах/пропусках и fail-closed при неизвестной NET-паре.
- Критические Telegram PANIC-события, независимый ACK монитор и dead-letter для неисправимых ответов Telegram.
- PANIC cleanup совместим с аномальной версией `0.7.23.1` при строгой проверке PANIC_DONE, мониторы не требуют ручного перечисления поддерживаемых clean-версий.
- Устойчивость read-only экранов к повреждённой записи реестра; управленческие команды продолжают отказывать на сомнительных данных.
- Установщик с pin SHA256 и обновлением по одному LIVE-боту, права файлов, документация безопасности.

Подробности, нерешённые ограничения и тестовая дисциплина — в [SAFETY_REVIEW.md](SAFETY_REVIEW.md).


## v0.7.29 — Maintenance / UI only (2026-10-24)
- ENGINE_VERSION stays 0.7.28; trading engine, FIFO, funding, strategy and state schema unchanged.
- Integrate already-LIVE verified observer provisioning for pristine unstarted bots.
- /status: fail-closed account total PnL from complete, fresh per-bot accounting; two decimals and round sign icons.
- Market SELL: compact header (base volume, number of closed grids, actual PnL indicator), execution deviation indicator and timing.
- Manager status: original bot uptime and independent process uptime.
- Current-version tests validate against metadata and manifest without literal release/engine pins.
- Existing legacy okx_live_upgrade.py is for 0.7.27 → 0.7.28 only. DO NOT use for v0.7.29.
- Package build/push does not update any running bot. Never copy okx_version.py into per-bot engine dependencies.

### Reader Diagnostics (v0.7.36 package; engine remains v0.7.35)

В главном меню `okxbot` пункт **Reader Diagnostics (весь аккаунт)** либо
`okxbot reader-diagnostics`. Один read-only экран показывает общий Reader IPC,
зарегистрированные инструменты и статистику маршрутов всех активных ботов.
Отдельных действий/экранов по каждому боту нет. Биржевые запросы и
systemd-операции управления из диагностики не выполняются.

`new`/`clone` обязаны использовать общий шаблон Reader и подтверждение
регистрации новой пары через GET-free Unix IPC до первого LIVE-старта.
Telegram сообщает одно BYPASS на подтверждённый эпизод и одно RECOVERED,
не принимая краткий обычный fallback за постоянный дефект.
