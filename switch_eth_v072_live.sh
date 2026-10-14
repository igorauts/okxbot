#!/usr/bin/env bash
# Explicit operator-initiated LIVE switch. Never call before isolated audit!
# Usage: sudo bash switch_eth_v072_live.sh --live
set -Eeuo pipefail
umask 077
[[ ${1:-} == '--live' ]] || { echo 'Refusing: pass --live after full tests and READ-ONLY audit';exit 2; }
PKG=$(cd "$(dirname "$0")" && pwd)
DIR=/root/okx/eth064
SERVICE=okx-grid-eth064.service
DROPIN=/etc/systemd/system/$SERVICE.d/99-v072.conf
PY="$DIR/.venv/bin/python"
STOPPED=0
START_ATTEMPTED=0
trap 'rc=$?;trap - ERR;echo "❌ SWITCH FAILED (rc=$rc)"; if [[ "$STOPPED" = 1 && "$START_ATTEMPTED" = 0 ]];then rm -f "$DROPIN";systemctl daemon-reload;systemctl start "$SERVICE" || true;echo "Attempted to restore old v0.7.1 service";else echo "Do not automatically downgrade after v0.7.2 startup. Diagnose state/OKX first.";fi;exit "$rc"' ERR
[[ $(id -u) == 0 ]]
systemctl is-active --quiet "$SERVICE"
systemctl is-active --quiet okx-grid-sol065.service
[[ $(systemctl show "$SERVICE" -p ExecStart --value) == *'okx_bot071.py'* ]]
[[ $(systemctl show okx-grid-sol065.service -p ExecStart --value) == *'okx_bot0610.py'* ]]
[[ ! -e "$DROPIN" ]]
cd "$PKG"
sha256sum -c SHA256SUMS >/dev/null
bash run_checks_v072.sh "$PY" >/dev/null
# Operator must have explicitly run audit_eth_v072.sh on current LIVE account.
# Safeguard: read the latest successful audit log, reject old/missing audits.
LATEST=$(ls -1t /root/eth072_audit_*.log 2>/dev/null | head -1 || true)
[[ -n "$LATEST" && -f "$LATEST" ]]
grep -q 'AUDIT CONSISTENT' "$LATEST"
AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST") ))
(( AGE >= 0 && AGE < 900 )) || { echo 'Audit older than 15 minutes; repeat audit_eth_v072.sh';exit 1; }
[[ -f "$DIR/okx_bot071.py" ]]
install -m 600 "$PKG/okx_bot072.py" "$DIR/okx_bot072.py"
cmp "$PKG/okx_bot072.py" "$DIR/okx_bot072.py"
"$PY" -m py_compile "$DIR/okx_bot072.py"
"$PY" - "$DIR/state/bot04_state.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]));assert s['version']=='0.7.1' and s['instrument']=='ETH-USDT-SWAP'
print('Before switch:',len(s['cycles']),'cycles',len(s.get('aggregate_exits',[])),'aggregate exits')
PY
STAMP=$(date -u +%Y%m%d_%H%M%S)
BACKUP="/root/eth064_before_v072_${STAMP}.tar.gz"
echo '=== STOP ETH ONLY ==='
systemctl stop "$SERVICE"
STOPPED=1
! systemctl is-active --quiet "$SERVICE"
# Snapshot state with SQLite WAL files, no concurrent writer.
tar -czf "$BACKUP" -C "$DIR" state config.json .env okx_bot071.py
[[ -f "$DIR/state/bot04_state.json" ]]
python3 - "$DIR/state/bot04_state.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]));assert s['version']=='0.7.1'
PY
install -d -m 755 "$(dirname "$DROPIN")"
cat > "$DROPIN" <<EOF
[Service]
ExecStart=
ExecStart=$PY -u $DIR/okx_bot072.py
Environment=OKX_RECON_PROFILE=1
EOF
chmod 644 "$DROPIN"
systemctl daemon-reload
[[ $(systemctl show "$SERVICE" -p ExecStart --value) == *'okx_bot072.py'* ]]
echo "Backup: $BACKUP"
echo '=== LIVE ETH v0.7.2 START ==='
START_ATTEMPTED=1
systemctl start "$SERVICE"
sleep 20
systemctl is-active --quiet "$SERVICE"
[[ $(systemctl show "$SERVICE" -p ExecStart --value) == *'okx_bot072.py'* ]]
"$PY" - "$DIR/state/bot04_state.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]));assert s['version']=='0.7.2' and s['instrument']=='ETH-USDT-SWAP'
assert s.get('cycles'),'Missing ETH cycles'
print('✅ ETH v0.7.2 state:',len(s['cycles']),'tracked cycles')
PY
systemctl is-active --quiet okx-grid-sol065.service
[[ $(systemctl show okx-grid-sol065.service -p ExecStart --value) == *'okx_bot0610.py'* ]]
echo '=== ETH RECENT LOGS ==='
journalctl -u "$SERVICE" --since "2026-10-14 19:00:00 UTC" --no-pager -o cat | tail -45
echo '✅ LIVE ETH v0.7.2 started. SOL unchanged.'
echo '⚠️ Watch live reconciliation, orders and exchange TP; success is NOT proof of complete safety.'
