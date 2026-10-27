# OKX Grid Bot — 0.7.34 OFFLINE RC3: Reader source integration

Дата подготовки: 26.10.2026. **НЕ ДЛЯ LIVE**, не устанавливать и не перезапускать systemd. Предназначен для исходного review/offline regression и доработки.

## Provenance
- База: точный пользовательский GitHub Release `v0.7.33`, commit `e48f2f043c831c6ec1f092d6c1926dbed622ed64`.
- Исходный архив SHA256 `b2eaf2e1d613b628c307c5ebf847c89b3fe0aff4bc8cc8e2a6a3760ed190d6b7`.
- Исходный `okx_bot.py` SHA256 `62202f16e683ef9a1ec91774f033af22d33b3fff2c6e3d4b86b7e86ded1efd97`.
- Кандидат имеет новую **трёхкомпонентную** engine/release identity из `okx_version.py`, source SHA закреплён в engine manifest. Ещё НЕ GitHub Release.

## Что добавлено
1. `okx_position_reader_core.py`: единый батч до 10 инструментов, Single-Flight запросов, индивидуальные monotonic freshness barriers, строгая проверка однозначности NET-позиции, bounded ожидания; нет торгового TTL.
2. `okx_position_reader_ipc.py`: Unix-socket transport в приватном каталоге, ограниченные сообщения, `SO_PEERCRED` (тот же uid); нет биржевых POST.
3. `okx_position_reader_service.py`: отдельный явно запускаемый **GET-only** кандидат сервиса. В чтении позиций использует **ОРИГИНАЛЬНЫЙ** `AccountPositionsFIFO`, а не второй rate limiter. API credentials не упакованы, путь к приватному 0600 credential file передаётся отдельно оператором. Новая служба **не создана и не запущена**.
4. `okx_position_reader_client.py`: synchronous IPC для торгового процесса, каждый запрос требует REST GET, начатого ПОСЛЕ запроса. Неверифицированная пустая строка по инструменту вызывает прежний индивидуальный GET через общий FIFO. Невозможность IPC в RUN вызывает `ExchangeTemporary`; в HARD/PANIC разрешён прежний индивидуальный GET с оригинальным emergency/normal gate (никакой второй независимой нормальной квоты).
5. `okx_api.py`: опциональный Reader в `API.positions()`. При отсутствии `OKX_POSITION_READER_SOCKET` в `.env` **точный прежний маршрут GET**. Никаких изменений POST/DELETE, virtual_escape, coalescing, coverage/TP/NET, частичных fills, PnL.
6. `okx_bot.py`: только передаётся callback `control_mode` в API; не меняются решающие функции стратегии. Исходный метод `API.positions` намеренно переаттестован через safety AST-файл, все прочие API/PnL guards прежние.
7. `okxbot.py`: сериализация создания/привязки через registry lock; лимит **максимум 10 неархивных/возобновляемых ботов** на аккаунт, включая STOPPED; исключаются лишь помеченные terminal+inactive по локальному реестру; отдельная биржевая flat-проверка остаётся обязательной перед LIVE. При некорректном реестре — fail-closed. При этом отдельный position ownership coordinator НЕ добавляется.
8. Сохранён `Rayls`→`RLS` в меню (пользователь уже применил этот пресет на LIVE отдельно).

## Что НЕ готово / препятствия к LIVE
- Критические приоритеты пока относятся только к расписанию Reader; старый межпроцессный файловый FIFO при mixed-version rollout остаётся без приоритетов. PANIC/EXIT не гарантировано быстрее старых GET. Нельзя объявлять P0 ускорение доказанным.
- Reader не проверен с реальным биржевым payload, n=0 при batched response компенсируется индивидуальным проверенным GET. Нужны безопасные GET-only проверки с реальным аккаунтом и соответствием credentials.
- Для старых архивных поколений реестр и terminal proof требуют отдельного аудита по VPS. Если они не подтверждены, Reader обязан отказаться запускаться.
- Динамический реестр при новых созданиях требует безопасного reload с дрейновой стадией. Сейчас Reader загружает реестр при запуске, смену набора требует отдельного управляемого перезапуска; автоматического reload нет.
- Строгий global multi-priority scheduler (Emergency>Exit>Normal>Background), starvation bounds и совместимость с emergency independent gate пока не доказаны. Аварийный fallback намеренно не обходит старую квоту. Отказ Reader во время LIVE без проверки безопасности оставит RUN fail-closed.
- Freshness window с повторным использованием торгового кэша **НЕ ВКЛЮЧЁН**. Ни 100/250ms, ни 1s TTL для POST.
- WS BID→Collect и влияния StatsWorker `fills` в этом RC **НЕ ускорены**; нужны связанная телеметрия, профилирование и следующий offline пакет.
- Сервис и новый IPC-клиент ещё не проверены на реальной OKX без POST, timeout/kill races и загрузке VPS. Конфигурация сервиса, systemd-unit, preflight/upgrader НЕ подготовлены — не выполнять существующий updater вслепую.
- Для LIVE безопасного использования нужен тест identity единого OKX account для всех ключей, а также биржевая GET-only сверка каждого инстанса.

## Проверки
- Прежний `run_checks.sh` по исходной 0.7.33 базе — PASS.
- Прежний `run_checks.sh` по интегрированной базе — PASS.
- Дополнительные новые tests `tests/test_position_reader.py`, `test_position_reader_integration.py`, `test_reader_manager_cap.py` — офлайн, fake provider/quota, temp Unix socket/temp FIFO и моковый менеджер; **не** свидетельство LIVE-ready.

## Следующие инженерные шаги
1. Разбор account-wide quota flow в текущей пятёрке, попытка приоритетов без срыва legacy FIFO, обязательно PANIC/HARD emergency.
2. Запись trace ID из WS crossing по всем `positions` FIFO/REST событиям с async bounded telemetry и разбор BID→Collect.
3. Reconcile точности READ-only финансов/позиции/TP для 5 инструментов, crash-restart, cancellation/GET timeout races; credential+registry attestations.
4. Профилирование StatsWorker `fills` и его конкуренции с loop/REST/CPU; zero behavior change к финансовым формулам.
5. Затем только отдельное разрешение пользователя на staged LIVE: backup + GET-only audit, первым ZEC, остальные позже, GitHub Release в конце.
