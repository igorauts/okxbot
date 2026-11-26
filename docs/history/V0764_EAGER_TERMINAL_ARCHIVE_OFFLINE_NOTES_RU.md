# v0.7.64 — OFFLINE ONLY; НЕ ДЕПЛОИТЬ

Исходный пакет v0.7.63, криптографический SHA256 закреплён отдельно при сборке.

## Scope
- Каждые 30 секунд после успешной полной сверки, в том же блоке что `compact_archived`, `eager_archive_terminals` наблюдает `CLOSED`/`EXITED` и ведёт отдельную durable map `terminal_seen: {cycleId: first_seen_unix}`.
- Не переносит до 900 с после первого увиденного terminal. Не меняет содержимое цикла, исходную структуру PnL ledger, BUY/TP/MARKET, финансовое ядро и Reader.
- Перед переносом требуется: терминальность, полный объём BUY=TP, PnL recorded, все TP фазы финальные, отсутствие частичного/manual cleanup, ссылок из escape_collection/незавершённых aggregate_exit, соответствующих pending OKX orders (включая исторические top-level tpOrdId/tpClOrdId).
- На переходе place() устаревшая запись terminal_seen удаляется в существующем сохранении intent. После выбранного переноса один durable state save; затем текущая compaction в SQLite; restart идемпотентен.
- LEGAL pending причины не считаются как `STATS_V2_STALLED`. Неподтверждённые причины продолжают считаться; в событии STALLED указываются причины.

## Тесты и ограничения
- Синтетические 4 цикла MARKET с общим ордером: 2 архив, 2 CLOSED в state. Проверка исходного FULL до/после eager, после compact, после restart, pending 2→0.
- 18 законных shared_component, 30 итераций: ноль ложных тревог; настоящие причины продолжают активировать счётчик.
- Обязательно перед LIVE: emergency battery, newbot/clone через Reader, deep/upper, тест реального BLUR state и per-bot canary без pending на несколько часов → BLUR → остальные, BTC последний.
- Указанный успех не гарантирует pending BLUR строго за ~20 мин, если запись ledger или TP статус не подтверждены. Не выполнять прямые SQLite/state правки на LIVE.

## Состояние проверки
- Проверено 17 новых тестов eager+STALLED, включая legacy attached TP, повторное открытие/закрытие, некорректную карту наблюдений, неизвестные aggregate-поля и неизменность financial_signature.
- Стенд 4 циклов выполнен на PINNED ORIGINAL FULL oracle.
- Исторические 164 phase + 32 production теста выполнились; один legacy AST-контракт обновлён для санкционированного eager-хука, финансовые тесты не изменялись.
- Полный `run_checks.sh` выполнен с EXIT=0: 724 теста в 61 отдельном блоке, включая изолированные multiprocessing Reader и 164+32 закреплённых финансовых теста. Первоначальный внешний таймаут полного запуска не воспроизвёлся при повторной проверке. Пакет всё ещё НЕ LIVE-ready, пока не проверены реальные BLUR-условия и не утверждена LIVE-канарейка.
- Emergency/newbot/clone/deep-grid регрессии прошли в составе полного `run_checks.sh`. Но фактический BLUR-снимок/ledger/pending order eligibility и поэтапная LIVE-канарейка остаются обязательными перед LIVE. Офлайн-снимок настоящего BLUR в данный пакет не включён.

## Safety notes RC2
- `terminal_seen` не влияет на финансовую сигнатуру и PnL identity; карта не содержит ордерные данные.
- Pending order check использует текущие `ordId`/`clOrdId` и сверяет их со всеми известными ID цикла, включая `tpOrdId` и `tpClOrdId`.
- Существующие 18 BLUR не гарантированно очистятся через 900 секунд: необходимы закрытые TP-фазы, ledger-recorded, отсутствие ссылок на незавершённые операции и реальных pending exchange orders.
- Текущие LIVE 8/8 движки и GitHub v0.7.63 не изменены этим пакетом.
