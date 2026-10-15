#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo 'Требуется root на VPS.' >&2
  exit 1
fi
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/okx-grid-v068
# Syntax and safety check before replacing the manager CLI.
/usr/bin/python3 -m py_compile "$SRC/okxbot.py" "$SRC/okx_setup068.py" "$SRC/okx_bot074.py" "$SRC/okx_monitor074.py" "$SRC/okx_execution_observer074.py" "$SRC/okx_ui074.py" "$SRC/okx_position_peak.py"
install -d -m 755 "$DST"
install -m 755 "$SRC/okxbot.py" "$DST/okxbot.py"
install -m 644 "$SRC/okx_setup068.py" "$DST/okx_setup068.py"
install -m 644 "$SRC/okx_bot074.py" "$DST/okx_bot074.py"
install -m 644 "$SRC/okx_monitor074.py" "$DST/okx_monitor074.py"
install -m 644 "$SRC/okx_execution_observer074.py" "$DST/okx_execution_observer074.py"
install -m 644 "$SRC/okx_ui074.py" "$DST/okx_ui074.py"
install -m 644 "$SRC/okx_position_peak.py" "$DST/okx_position_peak.py"
install -m 644 "$SRC/requirements.txt" "$DST/requirements.txt"
cat > /usr/local/bin/okxbot <<'SH'
#!/bin/sh
exec /usr/bin/python3 /opt/okx-grid-v068/okxbot.py "$@"
SH
chmod 755 /usr/local/bin/okxbot
printf '\nУстановлен OKX Grid Bot manager v0.7.4.\n'
printf 'Создание новых ботов: okxbot new (v0.7.4)\n'
printf 'Пауза: okxbot pause <name> | Возобновить: okxbot resume <name>\n'
printf 'Soft: okxbot soft <name> | Hard: okxbot hard <name>\n'
printf 'Panic: okxbot panic <name> | Клон завершённого: okxbot clone <old> <new>\n'
printf 'Старые экземпляры НЕ обновлялись и не перезапускались.\n'
printf 'Управление ордерами Pause/Soft/Hard/Panic доступно только движкам v0.6.8.\n'
