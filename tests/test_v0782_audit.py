"""Read-only UI and emergency visibility regressions. No LIVE operations."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_partial_attached_isolation import fixture, INST
from tests import test_v0781_audit as previous
import okx_stats_ui as ui
import okx_telegram_health as health
import okx_funding_view as funding

class ReserveVisibility(unittest.TestCase):
    def test_reserve_count_contract_quantity_and_age(self):
        st,_,_=fixture();st['instrument']=INST
        now=1795596770.7686288+3661
        self.assertEqual(ui.tp_reserve_text('/unused',INST,state=st,now=now),'1 / 2 контр.')
        self.assertEqual(ui.tp_reserve_text('/unused',INST,state=st,now=now,include_age=True),'1 / 2 контр. · 1ч 1м 1с')
        st['cycles']['0.3954']['tpOrders'][0]['phase']='ACTIVE'
        self.assertIsNone(ui.tp_reserve_text('/unused',INST,state=st,now=now))
    def test_duplicate_identity_not_reported_as_two_reserves(self):
        st,_,_=fixture();st['cycles']['duplicate']=st['cycles']['0.3954']
        self.assertIn('н/д',ui.tp_reserve_text('/unused',INST,state=st))
    def test_compact_and_full_keep_financial_total_and_report_reserve(self):
        case=previous.TelegramAudit();case.setUp()
        try:
            st,_,_=fixture();st['instrument']=INST
            (case.root/'state/bot_state.json').write_text(json.dumps(st))
            out,sink=case.render(None)
            self.assertIn('TP reserve: 1 / 2 контр.',out);self.assertEqual(sink,[20])
            with patch.object(case.m,'_status_funding_amount',return_value=None),patch.object(case.m,'refresh_bots'),patch.object(case.m,'_active_service',return_value=True),patch.object(case.m,'BOT_DIRS',{'enso':str(case.root)}),patch.object(case.m,'_verified_account_balances',return_value=None):
                report=case.m.compact_status_report()
                self.assertIn('TP reserve: enso 1 / 2 контр.',report)
                self.assertIn('TOTAL PnL: +$20.00',report)
                self.assertIn('⚠ TP reserve:',report)
                # A stale finance snapshot must not hide the independently
                # visible reservation or invent a financial total.
                case.stats['generated_at_unix']=time.time()-1000;case.write()
                report=case.m.compact_status_report()
                self.assertIn('TP reserve: enso 1 / 2 контр.',report)
                self.assertIn('TOTAL PnL: н/д',report)
        finally:case.doCleanups()
    def test_emergency_notice_is_deduplicated_and_not_terminal(self):
        for mode in ('HARD','PANIC'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as d:
                root=Path(d);(root/'state').mkdir();p=root/'state/control_ack.json'
                p.write_text(json.dumps({'mode':mode,'phase':'WAIT_RESERVED_TP','reserved_contracts':'2','remaining_position':'2','updated_at':'t'}))
                got=[];st={}
                with patch.object(health,'one',side_effect=lambda api,st,key,text:got.append(text)):
                    for _ in range(3):health.panic_ack_check(None,st,{'enso':'service'},{'enso':root})
                self.assertEqual(len(got),1);self.assertIn(mode+' WAIT_RESERVED_TP',got[0]);self.assertIn('НЕ подтверждено',got[0])
    def test_missing_db_not_never_synced(self):
        with tempfile.TemporaryDirectory() as d:
            t=time.time();x=funding.display_funding(Path(d)/'missing.sqlite',INST,t-1,now_ms=int(t*1000))
            self.assertTrue(x.unavailable);self.assertTrue(x.estimated);self.assertFalse(x.warning)
            self.assertIn('база недоступна',x.manager_line(int(t*1000)))
            self.assertNotIn('ещё не обновлялся',x.manager_line(int(t*1000)))

if __name__=='__main__':unittest.main()
