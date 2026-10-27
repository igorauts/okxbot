# OKX Grid Bot v0.7.34 — RC9 OFFLINE: немедленный wake при WS virtual TP и trace

**Не устанавливать на LIVE без отдельной проверки.** Источник — проверенный пользовательский ZIP v0.7.33 → RC8 → RC9. Никакие VPS-службы, OKX API или GitHub не менялись.

## Гипотеза задержки

В RC8 `PublicBidWatcher.run` уже записывал первое WS BID пересечение виртуального TP в `_virtual_crossed`, но будил основной цикл через `shared_wake` только при `grid_top(bid)>_grid_ref` и не чаще `PRICE_WAKE_COOLDOWN=0.5s`. Пересечение TP могло произойти без изменения grid-top или внутри cooldown, поэтому попадало в торговый цикл только при очередном пробуждении. Это подтверждённый исходный control flow, но фактический вклад в ZEC BID→Collect пока **не измерен**.

## Изменения RC9

1. Первое новое пересечение виртуального TP немедленно вызывает **только** `shared_wake.set()`. Повторное пересечение того же цикла и откат BID лишних wake не создают. При публикации targets сразу после полного REST reconciliation новое пересечение, уже видимое в свежем WS BID, тоже будит main loop. Из WS callback **не** создаются ордера, не вызывается POST, не меняется `bot_state.json`, не снимаются safety-guards. Grid wake cooldown 0.5s, торговое окно MARKET coalescing 0.15s и вся стратегия не меняются.
2. `okx_tp_telemetry.crossing_trace_id`: стабильный диагностический ID 24 hex символа из первого реально наблюдённого локального WS crossing timestamp + cycle ID, в лог передаётся только hash. При отсутствии реального пересечения ID не придумываем. Это **не** `clOrdId` и не торговое разрешение.
3. Reader IPC `trace_id` на стадии GET при известном crossing, сопоставимые labels в `rest_gate`/`positions_gate`/`http` для `GET /positions` и `GET /orders-pending` (в том числе когда Reader выключен). Метрики `tp_cross_to_collect`, `tp_collect_to_intent`, `tp_intent_to_submit` и события `AGG_MARKET_INTENT`/`AGG_MARKET_ACK` маркируются тем же ID; event logs содержат уже существующий `clOrdId` для связи с `AGG_MARKET_TERMINAL`/биржевым fill.
4. `okx_tp_trace_report.py <bot-root>` — GET-free офлайн-отчёт из ограниченных до 64 MiB хвостов `logs/latency.jsonl` и `logs/bot_events.jsonl`. Не вычисляет биржевую wire-latency, не суммирует медианы разных выборок и не трактует отсутствующий ACK как отказ ордера.
5. `tests/test_rc9_ws_wake_trace.py` проверяет реальный fake WS listen/run при **НЕ**изменившемся grid-top и cooldown, раннее пересечение после target publication, первый timestamp, отсутствие POST из WS, соответствие ID в intent, тестирование неизвестных timestamps, report; обновлены изолированные AST test fixtures для нового diagnostic helper. Обновлены только reviewed reference AST для `PublicBidWatcher`, `virtual_escape`, `API.positions` и SHA текущего engine в manifest; исторические SHA не менялись.

## Безопасность и честные ограничения

- Wake — только ускоряет реакцию. Перед MARKET остаются REST pending-orders→positions, reconciled ownership, coverage guards, durable intent fsync и отдельное окно 150 мс.
- Единый `trace_id` присваивается crossing cohort, а не каждому отдельному ордеру. При объединении нескольких целей позднее может измениться самый ранний выбранный crossing; такие случаи должны трактоваться как **неполная корреляция**, не приписываться по времени искусственно. В отсутствие WS stamps нельзя давать BID→Intent измерение.
- Async latency journal может drop samples; trace приближение, не финансовый источник истины.
- Ни StatsWorker CPU vs FIFO влияние, ни уменьшение BID→Intent P50/P95 на реальной OKX статистике в RC9 не доказано.
- Блокеры до LIVE сохраняются: полный сценарный аварийный audit, install/systemd Reader review, mixed fleet 24h GET-only & FIFO profiling, reconciliation/coverage/audits, явное разрешение контролируемого rollout ZEC-first. Reader в v0.7.34 выключен по умолчанию.
- Активные на момент последнего подтверждения ZEC/BTC/NEAR/WLD; RLS завершён PANIC по сообщению пользователя (отдельной биржевой GET-only проверки не было).
