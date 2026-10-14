#!/usr/bin/env bash
# Completely offline: no API calls, no systemd changes, no GitHub writes.
set -euo pipefail
cd "$(dirname "$0")"
PY="${1:-python3}"
"$PY" - <<'PY'
from pathlib import Path
import ast
for p in (Path('okx_bot073.py'),Path('okx_monitor073.py'),
          Path('okx_execution_observer073.py'),Path('prepare_repo_v073.py')):
    ast.parse(p.read_text())
print('✅ Syntax: engine, optional monitors, packaging')
PY
"$PY" -m unittest discover -s . -p 'test_v073_*.py' -q
"$PY" -m unittest -q test_universal_v073 test_universal_observer_v073 test_repo_patch_v073
printf '✅ v0.7.3 universal offline checks PASSED\n'
