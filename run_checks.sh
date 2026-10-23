#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -B tests/test_offline.py
python3 -B tests/test_emergency_hold.py
python3 -B tests/test_telegram_0722.py
python3 -B tests/test_v0723.py
python3 -B tests/test_v0723_final_ui.py
python3 -B tests/test_v0723_fifo_alert.py
python3 -B tests/test_v07231_telegram_start.py
python3 -B tests/test_v0724.py
python3 -B tests/test_v0725.py
python3 -B tests/test_v0726.py

python3 -B tests/test_v0727.py
python3 -B tests/test_v0728.py
