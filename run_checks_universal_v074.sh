#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
PY="${1:-python3}"
"$PY" - <<'PY'
import ast
from pathlib import Path
for name in ('okx_bot074.py','okx_monitor074.py','okx_execution_observer074.py',
             'okx_ui074.py','prepare_repo_v074.py'):
    ast.parse(Path(name).read_text(encoding='utf-8'))
print('✅ Syntax verified: trading engine, monitors, manager UI and publisher')
PY
# Run legacy v0.7.3 regression tests against the unchanged v0.7.3 engine
# when checking a complete GitHub checkout. They do NOT replace v0.7.4 tests.
if [[ -f test_v073_safety.py && -f test_universal_v073.py ]]; then
  "$PY" -m unittest discover -s . -p 'test_v073_*.py' -q
  "$PY" -m unittest -q test_universal_v073 test_universal_observer_v073 test_repo_patch_v073
fi
"$PY" -m unittest -q test_v074_rules test_v074_ui test_v074_patch
printf '\n✅ v0.7.4 OFFLINE TESTS PASSED — NO API, NO systemd, NO LIVE CHANGES\n'
