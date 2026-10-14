#!/usr/bin/env bash
set -euo pipefail
PYTHON=${1:-python3}
cd "$(dirname "$0")"
"$PYTHON" -m py_compile okx_bot0610.py okx_telegram_monitor.py prepare_repository_v0610.py
"$PYTHON" test_v0610_safety.py -q
"$PYTHON" test_telegram_monitor.py -q
"$PYTHON" verify_real_import.py
printf '\nPASS v0.6.10 offline checks\n'
