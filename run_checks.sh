#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -B tests/test_offline.py
python3 -B tests/test_emergency_hold.py
python3 -B tests/test_telegram_0722.py
