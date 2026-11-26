# OKX Grid Bot v0.7.34 — LIVE verified (2026-10-27)

Полный релиз с накопленными изменениями v0.7.31–v0.7.34; один OKX-аккаунт, линейные бессрочные USDT SWAP.

## LIVE подтверждение

- Четыре движка `zec0724`, `near0721`, `wld0728`, `btc0721` на v0.7.34, по отдельности обновлены на реальном VPS; инварианты engine/state, preflight, позиции OKX, real/virtual TP и отсутствие неизвестных ордеров — 4/4 PASS.
- Shared Position Reader RC12: один процесс получает снимки позиций для нескольких ботов (пакеты 1–3 инструментов наблюдались; не каждый запрос объединяется). Код только GET; используются существующие учётные данные, отдельный API-ключ не нужен.
- `mixed-fifo` остаётся активным, **priority FIFO не включён**. Клиенты автоматически обходят недоступный Reader и выполняют прямой GET через прежний общий лимитер; ограничение безопасности GET сохраняется.
- Reader: systemd автозапуск включён, `Restart=on-failure`, `RestartSec=3s`; уведомления Telegram RC13 `DOWN / RECOVERED / FAILED` включены. Стартовое информационное сообщение реально доставлено. Аварийные уведомления проверены офлайн, без намеренного падения LIVE.
- После полного подключения (2026-10-27 06:18:47 UTC): за 7.85 ч 116183 успешных IPC из 116185, P50 по отдельным ботам 60–67 мс и P95 216–226 мс (историческое наблюдение, не SLA). По ZEC 4 новых завершённых MARKET SELL; время WS BID пересечение виртуального TP → durable MARKET intent P50 558.4 мс, P95 692.8 мс, N=4. MARKET Telegram уведомления пользователем подтверждены. Не равно времени до фактического биржевого fill.

## Исходники релиза

- Базовый полный пакет RC11: SHA256 `98f515bc99189d07e11ce9e0361bfab577aa333f020392de67e92d604960dcad`.
- Рабочий Reader RC12 `okx_position_reader_service.py`: SHA256 `9f962e4d4419035dad0e607f20eabff224c83b42f4e13f050ca4a4e6d6ded3a4`.
- Telegram health RC13 `okx_telegram_health.py`: SHA256 `cbe9262405ac3656a40e646af44d1b997d2a251015b801e3a5a569381e1fa0bf`.
- `okx_telegram_monitor.py` в исходниках — точечная интеграция Reader в действующий LIVE Telegram-монитор v0.7.33, а не запасная неиспользуемая копия из `/opt/okx-grid-live`. LIVE hash проверяется издателем по SSH до публикации.
- `ENGINE_VERSION=RELEASE_VERSION=0.7.34`; engine SHA256 `edfd544e43839ac0554f2cf0a75a1f797f31931fc52020237cb5d0b574824fb3` без изменений после LIVE.

## Область изменений

- v0.7.31–v0.7.33: наблюдаемость TP и MARKET, надёжность financial snapshot / attached TP, StatsWorker, Telegram terminal health.
- v0.7.34: немедленное пробуждение WS для пересечений виртуального TP с защитой от wake storm; Shared Reader, пакетирование GET positions, IPC tracing и ограничители квоты; безопасный rolling mixed FIFO, Reader registry isolation; Telegram Reader DOWN/RECOVERED/FAILED.
- Стратегия сетки и лимитные защиты, схемы state и договорённость об одном аккаунте не изменены. Тесты используют source metadata, а не захардкоженные версии.

## Важное

Публикация не меняет LIVE VPS, не перезапускает торговых ботов и не выставляет ордера. Отдельные эксплуатационные параметры systemd и `.env` задаются на VPS и **не включают секреты** в публичный архив. Funding collector и priority FIFO в этот релиз не включены.
