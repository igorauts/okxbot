#!/usr/bin/env bash
# Read-only ETH reconciliation on an ISOLATED copy of current 0.7.1 state.
set -Eeuo pipefail
umask 077
PKG=$(cd "$(dirname "$0")" && pwd)
SRC=/root/okx/eth064
PY="$SRC/.venv/bin/python"
TMP=""
trap 'if [ -n "$TMP" ] && [ -d "$TMP" ]; then rm -rf -- "$TMP"; fi' EXIT
systemctl is-active --quiet okx-grid-eth064
systemctl is-active --quiet okx-grid-sol065
PID=$(systemctl show okx-grid-eth064 -p MainPID --value)
[[ $(systemctl show okx-grid-eth064 -p ExecStart --value) == *'okx_bot071.py'* ]]
[[ $(systemctl show okx-grid-sol065 -p ExecStart --value) == *'okx_bot0610.py'* ]]
cd "$PKG"
sha256sum -c SHA256SUMS >/dev/null
bash run_checks_v072.sh "$PY"
TMP=$(mktemp -d /root/eth072_isolated_audit.XXXXXX)
mkdir -m 700 "$TMP/state" "$TMP/logs"
install -m 600 "$SRC/config.json" "$TMP/config.json"
install -m 600 "$SRC/.env" "$TMP/.env"
install -m 600 "$PKG/okx_bot072.py" "$TMP/okx_bot072.py"
for f in "$SRC"/state/*.json; do
    [ -f "$f" ] || continue
    install -m 600 "$f" "$TMP/state/$(basename "$f")"
done
for name in bot04_events.jsonl tp_transitions_v057.jsonl; do
    if [ -f "$SRC/logs/$name" ]; then install -m 600 "$SRC/logs/$name" "$TMP/logs/$name"; fi
done
python3 - "$SRC/state" "$TMP/state" <<'PY'
import sqlite3,sys
from pathlib import Path
from urllib.parse import quote
source,target=map(Path,sys.argv[1:])
for name in ('pnl_v068.sqlite','fills_v068.sqlite','cycles_v068.sqlite'):
    original=source/name
    assert original.is_file(),name
    uri='file:'+quote(str(original),safe='/')+'?mode=ro'
    with sqlite3.connect(uri,uri=True,timeout=30) as old:
        with sqlite3.connect(str(target/name)) as new:old.backup(new)
    with sqlite3.connect(str(target/name)) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    print('✅ SQLite snapshot:',name)
PY
python3 - "$TMP/state/bot04_state.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]))
assert s['version']=='0.7.1',s['version']
assert s['instrument']=='ETH-USDT-SWAP'
print('ETH cycles before migration:',len(s['cycles']))
PY
# Test PURE MIGRATION on the isolated snapshot BEFORE GET reconciliation.
# Exchange can legitimately fill orders during read-only audit, so do not
# compare post-REST cycle states to the pre-REST snapshot.
env -u OKX_BOT04_LIVE -u OKX_BOT04_PROBE -u OKX_BOT05_STATS \
    OKX_BOT04_AUDIT=READ_ONLY OKX_RECON_PROFILE=0 \
    "$PY" - "$TMP/okx_bot072.py" "$TMP/state" <<'PY'
import importlib.util,json,sys
from pathlib import Path
engine=Path(sys.argv[1]); root=Path(sys.argv[2]); path=root/'bot04_state.json'
before=json.loads(path.read_text())
assert before['version']=='0.7.1'
spec=importlib.util.spec_from_file_location('isolated_eth_v072_migration',engine)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
after=m.ensure_state()
expected=dict(before,version='0.7.2')
assert after==expected,'Migration unexpectedly changed trading state'
assert json.loads((root/'bot04_state_before_v072.json').read_text())==before
assert after['cycles']==before['cycles']
print('✅ PURE ETH MIGRATION: cycle records, aggregate exits and profit preserved:',len(after['cycles']))
PY
STAMP=$(date -u +%Y%m%d_%H%M%S)
LOG="/root/eth072_audit_${STAMP}.log"
if ! timeout 240 env -u OKX_BOT04_LIVE -u OKX_BOT04_PROBE -u OKX_BOT05_STATS \
    OKX_BOT04_AUDIT=READ_ONLY OKX_RECON_PROFILE=0 \
    "$PY" -u "$TMP/okx_bot072.py" >"$LOG" 2>&1; then
    tail -65 "$LOG"
    echo "❌ READ-ONLY ETH AUDIT FAILED: $LOG"
    exit 1
fi
grep -q 'AUDIT CONSISTENT' "$LOG"
grep -E 'STATE_UPGRADED|Position contracts:|Open BUY:|Tracked states:|TP links resolved:|AUDIT CONSISTENT' "$LOG"
[[ "$PID" == "$(systemctl show okx-grid-eth064 -p MainPID --value)" ]]
systemctl is-active --quiet okx-grid-eth064
systemctl is-active --quiet okx-grid-sol065
echo '✅ ETH read-only audit PASSED; LIVE PID unchanged; SOL untouched'
echo "AUDIT_LOG=$LOG"
