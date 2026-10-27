# OKX Grid Bot v0.7.34 — единый OFFLINE RC5 (НЕ ДЛЯ LIVE)

Источник: точный пользовательский архив публичной v0.7.33 (SHA256 `b2eaf2e1d613b628c307c5ebf847c89b3fe0aff4bc8cc8e2a6a3760ed190d6b7`), с ранее подготовленной интеграцией Reader RC3. Все изменения кода/тесты здесь **офлайн**, без VPS и без обращений к реальному OKX.

## Подтверждённый scope

- Отдельный Account Position Reader с Unix socket, пакетный GET до 10 SWAP инструментов; максимум 10 неархивных ботов на одном NET-аккаунте, 1 пара = 1 бот.
- Только свежие GET. Результат, начатый раньше request freshness barrier, не подходит. Биржевая строка отсутствует → не считать позицию нулём без отдельной старой сверки.
- Один общий оригинальный аккаунтный квотный файл `/positions` с лимитом 8/2.1с; нет второй независимой квоты. Новое расширение `PriorityPositionsFIFO` использует также тот же файл FIFO и lock inode, совместимый со старыми билетами.
- Emergency > Exit > Normal > Background; старые билеты БЕЗ priority считаются EMERGENCY, поскольку в старом движке PANIC и обычный GET неразличимы для Reader; гарантировать приоритет старого PANIC важнее временного ускорения mixed-rollout. Для явно классифицированных новых Normal/Background возраст >=3с переводит их на уровень Exit для fairness; Emergency никогда не уступает из-за aging. Взаимодействие со старой очередью до/после уже начатого GET отдельно тестировалось только офлайн.
- Пассивный scheduling hint EXIT от WS crossing / pending virtual TP collection. Это НЕ разрешение на MARKET, не ускорение за счёт обхода существующего reconciliation. PANIC/HARD приоритет от существующего control mode.
- RLS `rls0733` пользователь сообщил закрытым через PANIC; не считать действующим. Независимого GET-only биржевого подтверждения его нулевой позиции в этой переписке нет.

## Добавлено после RC4

StatsWorker в `FillStore.ensure_expected` собирает per-thread замеры:

- `fills_sqlite_first`: первое чтение `fills.sqlite`;
- `fills_qty_index`: суммирование подтверждённых заполнений и определение отсутствующих объёмов;
- `fills_missing_recovery`: запрос только недостающих `ordId` через существующий `A.fills_by_order`, включая при необходимости REST/quota wait;
- `fills_sqlite_second`: повторное чтение SQLite, только если были недостающие заполнения;
- `fills_expected_orders` / `fills_missing_orders`.

Поля добавляются к существующей строке `⏱ StatsWorker: ...`; никаких новых HTTP/SQLite вызовов, дополнительных потоков, сохраняемых торговых файлов, изменений бухгалтерии, округлений, allocation или write-ahead policy. Нормальный BUY/TP/PANIC/Market API не меняется.

## Офлайн-результаты и честные ограничения

- Оригинальные `run_checks.sh` регрессии и новые тесты выполнены на исходной сборке, затем повторяются из чисто распакованного ZIP.
- Дополнительные тесты проверяют legacy/new FIFO совместимость, квотный лимит 8/2.1с, Emergency-vs-Normal (включая настоящие 3 OS процесса), callback EXIT, архивные registry ACK, StatsWorker missing/complete/no-expected fills без живых API.
- **НЕТ** доказанного ускорения BID→Intent или FIFO на LIVE и **НЕТ** доказанной причины 8.725с fills; для неё потребуется собрать новую разбивку стадий после отдельного контролируемого LIVE-разрешения.
- **НЕ ГОТОВО**: независимая проверка source diff / строгая проверка real OKX multi-pair payload, identity account credentials, systemd/upgrader, полное 5–10-process crash/failure injection и fairness, глобальная telemetry WS→FIFO→REST/HTTP→ACK/FILL, точное расследование BID→Collect, прочие замеры реальной производительности.
- Приоритетная очередь ещё может ограничиваться уже выданным token/HTTP call legacy-процесса. Не обещать прерывания существующего GET или мгновенного MARKET.
- Установку/остановку LIVE и публикацию GitHub без отдельного поручения запрещено проводить.
