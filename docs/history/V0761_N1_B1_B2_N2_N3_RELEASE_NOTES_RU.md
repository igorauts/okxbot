# v0.7.61 — Telegram reliability + state I/O maintenance — 6 ноября 2026

RELEASE_VERSION=0.7.61 · RELEASE_KIND=maintenance · ENGINE_VERSION=0.7.60.

## Изменения

- **N1:** Telegram health проверяет combined observer (текущую схему) и свежесть обоих summary, а не только старые burst/execution units; отслеживает отсутствие/падение ожидаемого combined unit. Старые сервисы используются только при подтверждённой legacy-схеме.
- **B1:** Telegram command bridge ограничивает повторы неисправной команды; durable dead-letter/incident позволяет остальным командам продолжаться, не пропуская подтверждение.
- **B2:** две независимые ~30-секундные checkpoint-политики для `offsets` журналов событий и BUY-zero cursor. `reader_route_scan_at` больше не вызывает постоянный fsync. Критические Telegram offset/ACK, outbox и оповещения остаются немедленно durable. Новый бот и перемотка курсора журналов требуют немедленного сохранения.
- **N2:** интерфейс управления observers показывает работающие combined units корректно.
- **N3:** устаревший watchdog timer исключён из пакета, legacy deploy путь не может вернуть timer-архитектуру; systemd шаблон с `Type=simple`, `--loop` и `[Install]` сохранён побайтово.
- **Тесты:** `tests/test_v0761_maintenance.py`, `tests/test_b2_offsets_checkpoint.py`, исправлена изоляция кеша S1 sampler и предыдущие latency discovery assertions; новые тесты добавлены в `run_checks.sh`.

## Проверено ДО публикации

- Полный `run_checks.sh` на staged исходниках VPS завершился `RC=0`. Дополнительные B2-тесты и полный офлайн-набор на обновлённом модуле завершились `EXIT=0` до LIVE.
- Первая LIVE maintenance установка: N1/B1/B2/N2/N3 успешно установлены; перезапущен только Telegram Monitor. Seven engine services, Reader, watchdog, combined observers неизменны.
- Финальное B2 исправление установлено отдельно с PINNED_OLD_SOURCE_SHA/PATCHED_NEW_SOURCE_SHA/ENGINE_SHA/LIVE preflight PASS, Telegram перезапущен, торговые PID неизменны.
- Read-only B2 аудит при реальной активности ZEC: 16 записей за 120 секунд до исправления event offsets -> 7 за 120 секунд после (−56.3% в наблюдаемых окнах). Это файловые перезаписи, не прямой счётчик fsync и не контролируемый A/B-тест.
- Оператор вручную подтвердил работу Telegram `/status` и `/full`.

## Границы безопасности

- Торговый движок, engine manifest, позиции, ордера, сеть OKX, алгоритмы BUY/TP/MARKET/EXIT и Shared Reader не менялись. ENG SHA256: `9c6cf1b46ba8f0b045a80ea97b3178657fd296c2e6d564fb8579839e66057cbd`.
- Публикация только синхронизирует уже проверенные LIVE-модули и офлайн-тесты с репозиторием. **Во время публикации тесты не запускаются**; никаких LIVE-записей или сервисных рестартов, OKX API-вызовов и повторных POST.
- `main` и один annotated tag публикуются атомарно. Если push прошёл, повторять publisher или переписывать тег запрещено; частичную публикацию требуется разбирать отдельно.
