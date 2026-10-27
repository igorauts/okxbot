# v0.7.35 — OFFLINE RC / НЕ УСТАНОВЛЕН В LIVE

**База:** официальная `v0.7.34`, сверенная с SHA256 релизного ZIP. Это source-кандидат для отдельного аудита и будущей контролируемой установки; он не является опубликованным релизом и не содержит криптовалютных секретов.

## Что изменилось

- P0: `PublicBidWatcher` — повторные BID upcross того же виртуального TP не могут инициировать wake чаще одного раза в секунду; первоначальное пересечение нового/переназначенного TP просыпается без дополнительного ожидания. First-cross timestamp не теряется. **Никаких MARKET POST из WebSocket**, coalescing 150 мс, подтверждения позиции/SELL TP не менялись.
- P1: Telegram Reader DOWN/RECOVERED/FAILED — ограничения частоты ошибок и FLAPPING с сохраняемым durable состоянием; аварийный FAILED остаётся видимым.
- P1: Reader различает безопасно доказанный terminal ACK при missing/failed unit и неопределённое состояние. Не принимается в архив только по отсутствию unit. Архивные финансовые файлы не изменяются.
- P1: Reader health IPC сообщает возраст in-flight GET и включён в диагностический watchdog. Отдельный ограниченный systemd timer предназначен только для Reader и **не стартует автоматически при сборке**.
- P1: Для Reader подготовлена конфигурация, читающая **существующий `/root/okx/.okxbot_account.env`** (тот же account key, без нового и без переноса). LIVE switch требует независимого UID/permissions preflight.
- P1: `okx_market_fill_audit.py` — исключительно локальный read-only анализ уже имеющихся MARKET событий и биржевых fills SQLite, без подстановки недостоверного clock offset.
- P2: шаблоны systemd/восстановление Reader и перенос 9 исторических RC-документов под `historical/`.

## Гарантии и границы

- Нет Funding Collector, Priority FIFO migration, новых account/API keys, subaccounts или Telegram торговых команд.
- Исходная стратегия и функции отправки POST/PANIC/state/TP не менялись; затронут класс public WS wake, Reader service/core/IPC, Telegram health.
- Не смешивать P50 WS→durable intent с задержкой first/last exchange fill. Исторически у ZEC после миграции было только четыре завершённых MARKET SELL — N=4, не SLA.
- В исходниках используется `--credentials-file`, а engine продолжает читать локальный `.env` экземпляра, как и раньше. Установка reader service должна использовать общий source без копирования секретов.
- Фактический Telegram entrypoint — `/root/okx/okx_telegram_monitor.py`; `/opt/okx-grid-live` содержит shared runtime, но может хранить неисполняемую копию Telegram.

## Перед любым LIVE

1. Внести изменения только после отдельного явного разрешения оператора.
2. Сверить `systemctl cat/show`, archive/stopped ZEC и DOOD, activity+heartbeat действующих NEAR/WLD/BTC, source SHA, root-owned account env без вывода секретов.
3. Проверить shared FIFO/quota и GET-only финансовое соответствие позиций, собственных реальных/виртуальных TP, неизвестных intents/orders.
4. Делать backup, staged `scp` Mac→VPS, отдельную Reader/Telegram миграцию, затем лишь разрешённый controlled one-bot LIVE. Telegram monitor обновлять только по реально импортируемым файлам `/root/okx`.
5. GitHub Release создавать **только после успешного LIVE** и только от `igorauts`, с SHA/whitespace/tag проверками и downloaded asset checksum. Это не задача RC.

## Packaging pre-LIVE correction (OFFLINE RC2)

- `okx_live_upgrade.py` and `tests/verify_release_asset.py` now explicitly accept only the documented `deploy/reader/` recovery templates (3-level paths); traversal, symlinks, unknown nested paths remain rejected. The first offline package was incompatible with both validators.
- The `okxbot.py` ZIP Unix executable mode is now `0755`, required by the existing fail-closed shared updater. This changes packaging metadata only, not manager source bytes.
- Dedicated regression for permitted and forbidden ZIP paths, without hardcoded release strings. Engine/source `okx_bot.py` bytes and the manifest engine hash are unchanged relative to RC1.
- Still NOT LIVE and NOT GitHub-published.
