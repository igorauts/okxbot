# Shared Position Reader — Telegram DOWN / RECOVERED / FAILED

Обновление **только** `okx_telegram_health.py` для существующего Telegram monitor.
Никаких изменений торговых движков, API-ключей, Reader IPC и FIFO. Новый Telegram webhook/bot не создаётся.

## События

- DOWN: Reader socket/health недоступен или служба systemd неактивна.
- RECOVERED: Reader снова systemd active и health IPC PASS.
- FAILED: Reader в systemd `ActiveState=failed`; сообщается `Result`.
- Обнаружение короткого падения между 5-секундными опросами: по увеличению `NRestarts`.
- Первое включение: один подтверждающий ℹ️ сигнал через существующую очередь Telegram, без имитации аварии.
- Накопленные старые `fetch_failed` и `quota_failed` принимаются как исходный уровень, без ложного предупреждения.
- Очередь сохраняется и отправляется существующим `okx_telegram_monitor.py`.

## Переключение на VPS

Бинарник сначала сверить SHA256 с сопровождающими инструкциями. Сохранить резервную копию исходного `okx_telegram_health.py`, установить замену атомарно, создать drop-in для `okx-telegram-monitor.service` с `Environment=OKX_POSITION_READER_SOCKET=/run/okx-position-reader/positions.sock`, выполнить `daemon-reload` и перезапустить **только** Telegram-монитор.

Не останавливать Reader и торговые службы ради тестирования оповещений.

## Офлайн-проверки

`python3 -B -m unittest discover -s tests -p 'test_reader_telegram_alerts.py' -v`

Файл автономного теста импортирует остальные Telegram-модули с установленного `/opt/okx-grid-live`.
