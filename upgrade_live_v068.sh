#!/usr/bin/env bash
# Upgrades ONE v0.6.7 LIVE instance to v0.6.8; no bulk exchange cancellations.
# Before the new LIVE service starts, audit failures roll back code/config/state.
# After LIVE starts, NEVER restore stale state automatically: exchange writes
# may already have occurred. Preserve evidence and require a manual audit.
set -Eeuo pipefail

[[ $(id -u) -eq 0 ]] || { echo 'Root required' >&2; exit 1; }
NAME="${1:-}"
[[ "$NAME" =~ ^[a-zA-Z][a-zA-Z0-9_-]{1,35}$ ]] || { echo 'Usage: bash upgrade_live_v068.sh eth064|sol065' >&2; exit 1; }
[[ "$NAME" == 'eth064' || "$NAME" == 'sol065' ]] || { echo 'Only tested live instances: eth064, sol065' >&2; exit 1; }
ROOT="/root/okx/$NAME"
UNIT="/etc/systemd/system/okx-grid-$NAME.service"
SERVICE="okx-grid-$NAME.service"
SOURCE="$(cd "$(dirname "$0")" && pwd)/okx_bot068.py"
OLD="$ROOT/okx_bot067.py"
NEW="$ROOT/okx_bot068.py"
PYTHON="$ROOT/.venv/bin/python"
CONFIG="$ROOT/config.json"
STATE="$ROOT/state"

for f in "$UNIT" "$SOURCE" "$OLD" "$PYTHON" "$CONFIG" "$STATE/bot04_state.json"; do
    [[ -f "$f" ]] || { echo "Missing $f" >&2; exit 1; }
done
systemctl is-active --quiet "$SERVICE" || { echo 'Service not running, nothing modified.' >&2; exit 1; }

"$PYTHON" - "$CONFIG" "$UNIT" "$PYTHON" "$OLD" <<'PY'
from pathlib import Path
import json,sys
cfg=json.loads(Path(sys.argv[1]).read_text())
unit=Path(sys.argv[2]).read_text()
expected=f'ExecStart={sys.argv[3]} -u {sys.argv[4]}'
assert cfg['version']=='0.6.7', 'Expected v0.6.7 config; stop'
assert cfg['name'] in ('eth064','sol065'), 'Unexpected bot name'
assert cfg['live_trading'] is True, 'Bot not LIVE'
assert expected in unit, 'Service does not execute the expected v0.6.7 engine'
print('PRECHECK OK:',cfg['name'],cfg['instrument'],
      'grid',cfg['grid_step'],'TP',cfg['take_profit'],
      'order_size',cfg['order_size'])
PY
"$PYTHON" -m py_compile "$SOURCE"
echo
printf 'This temporarily stops %s, backs up state, audits v0.6.8 READ-ONLY, then restarts it.\n' "$SERVICE"
printf 'Existing BUY / TP orders remain on OKX during the brief stop.\n'
read -r -p "Type exactly UPGRADE $NAME: " ANSWER
[[ "$ANSWER" == "UPGRADE $NAME" ]] || { echo 'Canceled.'; exit 1; }

BACKUP="$ROOT/upgrade_backups/v068_$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$BACKUP"
PRELIVE=1
STOPPED=0
restore_prelive() {
    local status=$?
    trap - ERR INT TERM
    if [[ "$PRELIVE" == 1 && "$STOPPED" == 1 ]]; then
        echo "Upgrade pre-LIVE failed; restoring v0.6.7 files and state from $BACKUP" >&2
        systemctl stop "$SERVICE" || true
        cp -a "$BACKUP/$SERVICE" "$UNIT" || true
        cp -a "$BACKUP/config.json" "$CONFIG" || true
        rm -rf "$STATE"
        cp -a "$BACKUP/state" "$STATE" || true
        systemctl daemon-reload || true
        systemctl reset-failed "$SERVICE" || true
        systemctl start "$SERVICE" || true
    elif [[ "$PRELIVE" == 0 ]]; then
        echo 'v0.6.8 was already started. DO NOT restore old state automatically; exchange may have changed.' >&2
        echo "Inspect: journalctl -u $SERVICE --no-pager -n 60" >&2
    fi
    exit "$status"
}
trap restore_prelive ERR INT TERM

systemctl stop "$SERVICE"
STOPPED=1
if systemctl is-active --quiet "$SERVICE"; then
    echo 'Service did not stop cleanly' >&2
    false
fi
cp -a "$UNIT" "$BACKUP/$SERVICE"
cp -a "$CONFIG" "$BACKUP/config.json"
cp -a "$STATE" "$BACKUP/state"
cp -a "$OLD" "$BACKUP/okx_bot067.py"
install -m 600 "$SOURCE" "$NEW"
"$PYTHON" - "$UNIT" "$OLD" "$NEW" "$CONFIG" <<'PY'
from pathlib import Path
import json,os,sys,tempfile
unit,old,new,config=map(Path,sys.argv[1:])
s=unit.read_text()
assert str(old) in s
unit.write_text(s.replace(str(old),str(new)))
c=json.loads(config.read_text())
assert c['version']=='0.6.7'
c['version']='0.6.8'
c.setdefault('partial_buy_timeout_sec',60)
c.setdefault('partial_buy_distance_steps',5)
fd,tmp=tempfile.mkstemp(dir=config.parent,prefix='.config-v068-')
try:
    with os.fdopen(fd,'w') as f:
        json.dump(c,f,indent=2,ensure_ascii=False)
        f.flush();os.fsync(f.fileno())
    os.chmod(tmp,0o600)
    os.replace(tmp,config)
finally:
    if os.path.exists(tmp):os.unlink(tmp)
PY
systemctl daemon-reload
printf '\n=== v0.6.8 READ-ONLY AUDIT (never sends/cancels orders) ===\n'
env -u OKX_BOT04_LIVE -u OKX_BOT04_PROBE -u OKX_BOT05_STATS \
    OKX_BOT04_AUDIT=READ_ONLY "$PYTHON" -u "$NEW"
printf '\nREAD-ONLY AUDIT PASSED. Starting v0.6.8.\n'
# Once systemd starts new engine, state must NEVER be rolled back blindly.
PRELIVE=0
systemctl reset-failed "$SERVICE" || true
systemctl start "$SERVICE"
sleep 8
systemctl is-active --quiet "$SERVICE" || { echo 'New engine did not remain active; inspect journal.' >&2; false; }
trap - ERR INT TERM
printf '\nUPGRADE SUCCESS: %s v0.6.8 | backup %s\n' "$NAME" "$BACKUP"
systemctl status "$SERVICE" --no-pager -l --lines=12
