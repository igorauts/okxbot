> **Обновление 26.10.2026:** эта функциональность вошла в установленный на четырёх LIVE-ботах движок 0.7.33. DOOD закрыт отдельной аварийной PANIC-процедурой: нулевая позиция, итоговый торговый PnL проверен по подтверждённым исполнениям, терминальный статус PANIC_DONE. Последующий текст фиксирует прежний офлайн-этап.

# OKX Grid Bot v0.7.32 — OFFLINE RC2: StatsWorker + attached TP failures

**Статус: OFFLINE-КАНДИДАТ, НЕ УСТАНОВЛЕН И НЕ ПРОВЕРЕН НА LIVE.** База: v0.7.32 Financial Snapshot RC1, engine v0.7.32. До этого v0.7.31 использовался только на ZEC, остальные торговали на 0.7.30.

## Подтверждённый DOOD-инцидент (26 октября, UTC)

- Filled parent BUY по 0.002280, 1 контракт (1000 DOOD). Плановый LIMIT TP 0.002290.
- `attachAlgoOrds[].failCode=51006`: OKX потребовала `min sell price: 0.002292`.
- Child SELL не создан, в локальном state осталось `PENDING` без ordId, блокировка новых BUY без явного статуса. До окончания диагностики на OKX оставалась позиция 1 контракт и 0 открытых ордеров.
- **Эта сборка НЕ закрывает DOOD**. DOOD требуется отдельная PANIC-процедура с проверенным нулём позиции, отсутствием ордеров и final_stats. Не обновлять этот бот в обычном rollout и не менять его state вручную.

## Поведение следующего подтверждённого 51006

1. REST order(cid) и paginated history для attached TP. Если TP обнаружен, независимо от failCode обрабатывается как реальный SELL с точной проверкой identity, размера и исполнения.
2. Если SELL не найден, сверить exact parent BUY: instrument, side, ordId, clOrdId, filled quantity, attached client ID, kind=limit, target TP и явный `failCode`.
3. Только явный ненулевой `failCode`, подтверждённый родителем, позволяет terminal transition `attached PENDING → FAILED` с fsync и журналом; цикл становится `VIRTUAL_TP` с **исходным tpPx без модификации**. При отсутствии такого доказательства WAIT (no duplicate SELL, no new BUY).
4. Если биржевых real TP уже не меньше `real_tp_target`, виртуальный TP остаётся виртуальным. Иначе на свежем GET price-limit сравнить исходную цену с `sellLmt`. При цене ниже лимита — оставить виртуальным (retry check с cooldown). Никаких поправок `pxAmendType`/поднятия цены.
5. Если цена допустима и есть свободный слот, восстановить независимый reduceOnly LIMIT SELL по **исходной цене TP**, с новым durable clOrdId и только после полного coverage/positions reconciliation. POST outcome UNKNOWN никогда не повторяется вслепую. Жёсткий 150-order cap сохраняется. При лимите слотов и невозможности разместить LIMIT виртуальная MARKET TP-механика работает как прежде.
6. `51008_*` от parent attachment вызывает account-wide SOFT latch. Другие неподдерживаемые `failCode` дают сохранённый `tpFailureManualReview` + `BUY_HOLD_TP_FAILURE` и уведомление, пока виртуальное закрытие ещё возможно. Не классифицировать `51603` как доказательство отказа.
7. По длительному неподтверждённому attached TP RUN может показывать `BUY_HOLD_RECONCILIATION` вместо ложного зелёного статуса. Рестарты/PANIC не вызываются автоматически.

## Старые долги, включённые из RC1

- Согласованный financial snapshot при непрерывных BUY/SELL и race REST/fills; проверка только целых фактических fills; PnL ledger по соответствующим CLOSED cycles; повтор неудачного stats после 60 сек с достоверной временной меткой.
- MARKET SELL Telegram single color, TP WS BID→Intent telemetry (от v0.7.31). Не изменены MARKET coalescing 150ms, account FIFO 8/2.1s, PANIC/HARD, стратегия сетки, наращивание размера, schema=1.

## Безопасность и внедрение

- Тестовый пакет: offline only, без обращения к приватной OKX, без фактической торговли. Добавлены тесты для 51006, 51008, 51000, partial canceled BUY, плохого parent identity, price limits, real TP cap, запрета отменять существующий TP из-за недопустимой цены виртуального.
- `place_virtual_tp` является защищённой функцией: её pinned AST fingerprint изменён **целенаправленно** после добавления price-limit guard, с отдельными тестами на отсутствие дополнительного POST и на сохранение цены TP; остальные защищённые функции не изменены.
- Следующий этап: independent source audit, GET-only VPS preflight и staged controlled LIVE upgrade **сначала ZEC**. Отдельная PANIC-процедура для DOOD не зависит от установки v0.7.32.
- Реальная гарантия работы и восстановления на активно торгующем VPS **пока не подтверждена**.
