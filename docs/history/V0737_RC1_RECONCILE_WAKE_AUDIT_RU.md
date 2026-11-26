# OKX Grid v0.7.37 OFFLINE RC1 — диагностика причин сверки

**Статус: OFFLINE ONLY; NOT LIVE; NOT APPROVED FOR LIVE INSTALLATION.**

Основа: проверенный архив `okx_grid_v0736_final.zip`, engine 0.7.35.
Это диагностическая сборка с изменёнными байтами `okx_bot.py`, поэтому по контракту новая версия движка и пакета **0.7.37** (ровно три компонента). Публикации GitHub нет. Торговые параметры, очередь `mixed-fifo`, число ордеров, TP и правило свежего GET **не изменены**.

## Что выяснили по DOOD без изменения кода

- ~4489 полных reconcile за час, ~4539 Reader GET (один GET на проход с учётом повторов).
- ~98.9% вызовов reconcile в один проход; 49 multi-pass.
- Reader GET P50 51.5 ms, P95 284.1 ms; внутри ожидание quota P95 273.1 ms, HTTP P95 33.3 ms.
- В коде есть обязательный таймер ожидания до 1 секунды и ранние пробуждения: private orders WS, пересечения виртуальных TP, повышение верхнего уровня BUY-сетки; частые проходы не доказанно лишние.

## Изменения RC1

1. `PublicBidWatcher`: только счётчики `grid` и `virtual` wake внутри существующего lock. Они НЕ создают новых пробуждений, не управляют ордерами и не изменяют WS latch.
2. `live_loop`: классификатор `private / virtual / grid / timer / wake / internal / overrun / recovery`, только `LATENCY.record('reconcile_trigger',0, source=...,reason=...,count=...)` сразу перед реальной сверкой. Полная сверка по-прежнему выполняется каждый раз, без пропусков.
3. `okx_reconcile_trigger_report.py`: read-only сводка корреляции wake причины и длительности позиции по `latency.jsonl`; не обращается к OKX. Причины одновременных пробуждений сохраняются в поле `reason`, приоритетная причина в `source`. Метки `wake`/`internal` бывают неоднозначны, поэтому **точно считать их лишними нельзя**.
4. Дополнительные офлайн-юнит-тесты. Зафиксирован новый AST-хеш `PublicBidWatcher` после отдельного обзора: изменения только диагностические.

## Что НЕ сделано

- Никакой оптимизации уменьшающей GET: пока нет достаточных данных, чтобы гарантировать одинаковую скорость BUY refill при пропущенном приватном WS и ценовых пересечениях. Преждевременное отключение сверки на LIVE опасно.
- Не меняли `INTERVAL=1.0`, `ESCAPE_COALESCE_SEC`, cooldown, `Priority FIFO OFF`, аварийные ветки, TP coverage, PANIC, durable intent и сетку.

## Порядок следующего решения

Только после отдельно согласованного LIVE preflight/канареечного развёртывания именно на DOOD и накопления данных `reconcile_trigger` (никаких автоматических рестартов по результатам) сравнить P50/P95/P99 BUY-refill и частоты GET, выделить действительно избыточные *безопасные* проходы. Сохранить fail-closed и обязательную сверку перед trade POST. Остальные боты не участвуют в диагностике.

## Результаты локальных офлайн-проверок RC1

- **47/47** профильных тестов (wake attribution, WS, TP, Reader Diagnostics, release guards) — PASS.
- **150/150** тестов Reader / FIFO / mixed rollout / resilience / WS / telemetry / newbot — PASS (не включая отдельно описанный ниже спорный мультипроцессный priority-тест).
- Контрактный `tests/test_offline.py` PASS (включая pinned safety AST для 26 символов).
- Синтетический прогон `okx_reconcile_trigger_report.py` PASS: пять пар trigger/reconcile, ноль unmatched.
- Полный `run_checks.sh` **НЕ PASS**: один исходный тест `tests.test_priority_multiprocess_rc4.SharedQuotaMultiprocess.test_emergency_preempts_queued_normal_without_quota_bypass` нестабилен. На исходном v0.7.36 он воспроизводимо падал в 3/3 повторов, на RC1 падал в 2/3 и проходил в 1/3. При текущем `mixed-fifo / Priority FIFO OFF` это непроизводственный optional priority-путь, но **тестовый блокер не скрываем**. Не меняли scheduler, условие теста или FIFO-код ради зелёной галочки. До LIVE полный прогон должен быть разъяснён/исправлен и выполнен успешно.
