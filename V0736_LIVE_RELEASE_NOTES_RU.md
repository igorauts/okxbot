# OKX Grid Bot v0.7.36 — LIVE verified (2026-10-28 UTC)

## Состояние и границы

**Пакет 0.7.36, торговый движок 0.7.35.** `okx_bot.py` и торговая стратегия не менялись, новая версия движка не приписывается maintenance-обновлению. Приоритетный FIFO не включён (`mixed-fifo`), Funding Collector и TP wake cooldown не изменены. Архивные боты не запускались.

## LIVE rollout — подтверждения оператора

- Принятый пакет: `OKX_GRID_BOT_v0736_OFFLINE_RC1_NOT_LIVE.zip`, проверка SHA256 и read-only preflight — PASS.
- Shared Reader и Telegram обновлены контролируемо, с бэкапом `/srv/okxbot-live-backups/20261028T012004.290755Z-shared-runtime`.
- Reader: `ACTIVE`, health IPC `OK`, зарегистрированы `BTC-USDT-SWAP`, `DOOD-USDT-SWAP`, `NEAR-USDT-SWAP`, `WLD-USDT-SWAP`; `mixed-fifo`; watchdog `ACTIVE`.
- Telegram-monitor: `ACTIVE` после контролируемого рестарта.
- Движки NEAR / WLD / BTC / DOOD остались `ACTIVE` с прежними PID `2443682`, `2451244`, `2451381`, `2538599` соответственно. **Торговые движки не перезапускались.**
- `okxbot reader-diagnostics` показал единый account-wide экран, четыре `READER OK` и `registered: YES`. Локальная проверка не выполняла новых биржевых GET.
- Оператор получил `LIVE READER DIAGNOSTICS PATCH PASS — NO TRADING ENGINE RESTART` и `V0.7.36 LIVE INSTALL COMPLETE`.

## Изменения

- P0: `new` / `clone` / `start` — наследование текущей конфигурации Reader, GET-free IPC-регистрация нового бота с проверкой владельца и пары; fail-closed при непроверенной регистрации.
- P1: Telegram BYPASS / RECOVERED с устойчивой диагностикой, дедупликацией и без ложного аварийного алерта при кратком Reader restart.
- P2: исправлены формулировки новых эпизодов GET/quota и исполняемые биты CLI-скриптов.
- Новый общий экран: `Reader Diagnostics` для всего аккаунта — без торговых команд, API POST и биржевых GET.

## Ограничения проверки

- Показанные IPC-счётчики — **не число реальных биржевых GET**; GET/quota счётчики Reader обнуляются при перезапуске.
- Для уведомлений BYPASS / RECOVERED пройдены офлайн-сценарии; **не проводилась искусственная авария LIVE Reader** и не подтверждалась фактическая доставка при будущей аварии.
- Сквозной LIVE-тест создания/запуска ещё одного нового торгового бота не выполнялся; соответствующие офлайн-тесты входят в пакет.
- Публикация GitHub происходит отдельно после проверки соответствия LIVE исходникам; финальный ZIP отличается от установленного RC1 исключительно документацией.
