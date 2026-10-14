#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=${1:-python3}
"$PYTHON" -m py_compile okx_bot072.py test_v072_*.py verify_import_v072.py verify_upgrade_v072.py
for t in safety profiling aggregate state buffer crossing; do
  "$PYTHON" "test_v072_${t}.py" -q
done
"$PYTHON" test_v072_near_bid.py -q
"$PYTHON" verify_import_v072.py
"$PYTHON" verify_upgrade_v072.py
