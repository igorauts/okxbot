# v0.7.34 — RC4 OFFLINE: приоритетный Shared Position Reader

**Статус: ТОЛЬКО ОФЛАЙН. НЕ УСТАНАВЛИВАТЬ НА LIVE VPS. НЕ ПУБЛИКОВАТЬ В GITHUB.**

База: точный опубликованный v0.7.33, пользовательский ZIP SHA256 `b2eaf2e1d613b628c307c5ebf847c89b3fe0aff4bc8cc8e2a6a3760ed190d6b7`, интеграция RC3. Исходные trading/PANIC safeguards сохранены; все изменения относятся к опциональному Reader и приоритетной выдаче GET-разрешений.

## Реализовано в RC4

1. **Совместимый приоритетный FIFO:** новый `okx_positions_priority.py` расширяет оригинальный `AccountPositionsFIFO` в том же quota-файле, в том же queue-файле и под тем же `flock`, не создавая новой или повышенной квоты. Оригинальный `AccountPositionsFIFO` при выключенном Reader продолжает старое FIFO-поведение без изменения формата создаваемых им tickets. Новая очередь может переставлять tickets по priority, включая старых клиентов, не превышая текущий лимит **8/2.1с**.
2. **Emergency > Exit > Normal > Background:** дополнительные ticket-поля обратно читаются старым кодом; старые tickets без поля priority консервативно считаются Emergency (иначе можно было бы вытеснить PANIC старого движка). Обычные/background запросы старше 3с повышаются до уровня Exit, сохраняя Emergency выше всех; корректность/ограниченность такой fairness-policy ещё требует стресс-тестов и LIVE измерений после отдельных разрешений.
3. **Динамическое повышение:** пока Reader ждёт квоту, вновь обнаруженный срочный EXIT или PANIC меняет priority текущего reader-ticket при следующем polling FIFO. Уже выданную квоту и отправленный запрос никто не прерывает.
4. **Реальный scheduling hint от движка:** в `main()` после создания `PublicBidWatcher` устанавливается `A._reader_exit_hint`, который только читает наличие локально наблюдённых virtual TP crossings и pending `escape_collection`/`virtualTpCrossed`. Этот hint **не запускает ни один POST** и не обходится полный REST/NET/coverage/unknown-intent audit. При ошибке hint выбор NORMAL безопасно сохраняется. Emergency берётся только из существующего `control_mode()`.
5. **Архивные боты:** Reader сверяет `retired` + `state/control_ack.json` (`PANIC_DONE`/`RETIRED`) + systemd inactive для локального исключения из общего количества неархивных ботов. Биржевой ноль позиции/ордеров **должен отдельно подтвердить GET-only preflight** при LIVE rollout. RLS пользователь сообщил завершённым через PANIC, новый биржевой аудит в чате не выполнялся.
6. **Тесты:** добавлены 8 приоритетных FIFO негативных/позитивных сценариев, реальный многопроцессный стресс (3 клиента при полностью занятой квоте, Emergency обгоняет Normal), тесты архивного реестра и пассивного EXIT hint. Собственные торговые файлы/state/SQLite, MARKET batching 150мс и математическая торговая стратегия не менялись.

## Принципиальные ограничения и риски

- Нет доказанного ускорения BID→Intent: никакие реальные GET, торговые команды или VPS-модификации для RC4 не выполнялись. Исторический P50 ZEC=2478мс, FIFO P50=1285мс — не результат RC4.
- Невозможность preempt уже удерживаемого legacy lock/готового HTTP GET; priority только влияет на **очередь до получения квоты**. Со старым бинарником и смешанными версиями нужно отдельное моделирование с настоящими процессами, отменами и crash injection.
- Reader пока не выполняет регулярный собственный polling/TTL, потому что **кэшированный снимок не утверждён для торгового решения**; текущие запросы строго fresh-GET по barrier.
- Нет GET-only аттестации живого аккаунта / uniform account UID ключей / полного безопасного updater / production systemd unit.
- Новую telemetry correlation WS→FIFO→REST и разделение StatsWorker fills latency на этапы продолжаем отдельно; это не законченные задачи RC4.
- Глобальный POST safety / PANIC priority и starvation behaviour требуют независимого source-аудита перед любым LIVE-тестом.

**Следующий этап:** независимый аудит source diff и state-contract, offline стресс 5–10 процессов с 1 quota-file, потерянный ACK, crash/restart, PANIC и simultaneous TP; WS→Collect trace; SQLite/REST/CPU конкурентность StatsWorker; затем отдельное согласование оператора на контролируемый ZEC LIVE rollout.
