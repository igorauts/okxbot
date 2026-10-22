"""Offline regression for missing control.json + valid RUNNING ack; no network or live data."""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import okx_telegram_health as health


def case(label, control, ack='RUNNING', fresh=True, expected=False):
    with tempfile.TemporaryDirectory(prefix='okx-v07231-start-') as directory:
        bot = Path(directory)/label
        state = bot/'state'
        state.mkdir(parents=True)
        if control != 'missing':
            (state/'control.json').write_text(control, encoding='utf-8')
        if ack is not None:
            (state/'control_ack.json').write_text(json.dumps({'phase': ack, 'mode': 'RUN'}))
        if fresh:
            (state/'runtime_stats.json').write_text(json.dumps({'last':time.time()}))
        store={'systemd_watch_v0722':{label:{'active':True,'invocation':'old','cursor':'cursor0'}}}
        notices=[]
        def once():
            with patch.object(health,'_show',return_value={'ActiveState':'active','InvocationID':'new'}), \
                 patch.object(health,'_journal',return_value=[]), \
                 patch.object(health,'one',side_effect=lambda api,st,key,msg:notices.append(msg)):
                health.service_check(None,store,{label:f'okx-grid-{label}.service'},{label:bot})
        once()
        assert not any('STARTED — RUN' in x for x in notices), (label,notices)
        if fresh:
            time.sleep(0.03)
            (state/'runtime_stats.json').write_text(json.dumps({'last':time.time()}))
        once()
        assert any('STARTED — RUN' in x for x in notices) == expected, (label,notices)

case('missing-control','missing',expected=True)
case('explicit-run','{"mode":"RUN"}',expected=True)
case('corrupt-control','{BROKEN',expected=False)
case('invalid-control','{"mode":"INVALID"}',expected=False)
case('pause-control','{"mode":"PAUSE"}',expected=False)
case('missing-ack','missing',ack=None,expected=False)
case('missing-heartbeat','missing',fresh=False,expected=False)
print('PASS: v0.7.23.1 Telegram START/RUN missing-control and fail-closed regression')
