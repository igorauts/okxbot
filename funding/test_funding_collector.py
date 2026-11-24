import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from okx_funding_collector import (inventory, BillsClient, fetch_window, run_once,
    plan_windows, archive_floor, RECENT, ARCHIVE, DAY, HOUR, read_state, LedgerError)
from okx_funding_ledger import read_snapshot
from test_funding_ledger import bill, MMT, ETH, T

NOW=T+300000
END=NOW-120000
CREDS=dict(OKX_API_KEY='fake',OKX_API_SECRET='fake-secret',OKX_API_PASSPHRASE='fake-pass')

class FakeClient:
    def __init__(self, rows, fail_at=None):
        self.rows=rows; self.requests=0; self.calls=[];self.fail_at=fail_at
    def page(self, endpoint, begin, end, after):
        self.requests+=1; self.calls.append((endpoint,begin,end,after))
        if self.requests==self.fail_at: raise LedgerError('SIMULATED_FAILURE')
        return sorted([r for r in self.rows if begin<=int(r['ts'])<end and (after is None or int(r['billId'])<int(after))],key=lambda r:int(r['billId']),reverse=True)[:100]

class CollectorTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.db=self.root/'funding.sqlite'
        self.bots={MMT:(T-DAY)/1000};self.inv=lambda:(self.bots,CREDS)
    def run_fake(self, rows, **kwargs):
        c=FakeClient(rows,kwargs.pop('fail_at',None));return run_once(self.db,self.inv,lambda _:c,now=kwargs.pop('now',NOW),**kwargs),c
    def test_backfill_then_hourly_only_day(self):
        result,c=self.run_fake([bill(1)],mode='backfill');self.assertEqual(result['requests'],1)
        result,c=self.run_fake([bill(1)],now=NOW+HOUR);self.assertTrue(result['reconcile'])
        result,c=self.run_fake([bill(1)],now=NOW+2*HOUR);self.assertFalse(result['reconcile']);self.assertEqual(result['requests'],1)
        self.assertEqual(c.calls[0][2]-c.calls[0][1],DAY)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'-0.00172725')
    def test_full_pages_equal_timestamp(self):
        c=FakeClient([bill(i) for i in range(1,201)])
        a,b,rows=fetch_window(c,RECENT,T-1,T+1);self.assertEqual(len(rows),200);self.assertEqual(c.requests,3)
        self.assertEqual([v[3] for v in c.calls],[None,'101','1'])
    def test_partial_second_page_no_publish(self):
        self.run_fake([bill(1)],mode='backfill');before=read_snapshot(self.db,MMT,self.bots[MMT])
        with self.assertRaisesRegex(LedgerError,'SIMULATED_FAILURE'):
            self.run_fake([bill(i) for i in range(1,202)],fail_at=2,now=NOW+HOUR)
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT]),before)
    def test_repeated_cursor_rejected(self):
        c=FakeClient([bill(i) for i in range(1,101)])
        c.page=lambda *args: list(reversed(c.rows))
        with self.assertRaisesRegex(LedgerError,'REPEATED_PAGE_BILL'):fetch_window(c,RECENT,T-1,T+1)
    def test_ten_day_outage_archive_overlap(self):
        self.run_fake([bill(1)],mode='backfill')
        now=NOW+10*DAY
        result,c=self.run_fake([bill(1),bill(2,ts=T+DAY)],now=now)
        self.assertIn(ARCHIVE,[p[0] for p in c.calls]);self.assertIn(RECENT,[p[0] for p in c.calls])
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'-0.0034545')
        self.assertTrue(read_snapshot(self.db,MMT,self.bots[MMT])['complete'])
    def test_old_history_is_not_claimed(self):
        self.bots[MMT]=(NOW-100*DAY)/1000
        result,c=self.run_fake([],mode='backfill')
        self.assertEqual(c.calls[0][1],archive_floor(NOW));self.assertFalse(read_snapshot(self.db,MMT,self.bots[MMT])['complete'])
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'0')
    def test_new_bot_identity_and_foreign(self):
        self.run_fake([bill(1),bill(2,ETH)],mode='backfill')
        old=self.bots[MMT];self.bots[MMT]=(T+1000)/1000
        self.run_fake([bill(1),bill(2,ETH),bill(3,ts=T+1000)],now=NOW+HOUR)
        self.assertIsNone(read_snapshot(self.db,MMT,old))
        self.assertEqual(read_snapshot(self.db,MMT,self.bots[MMT])['amount'],'-0.00172725')
    def test_inventory_changes_precommit(self):
        vals=iter([(self.bots,CREDS),({ETH:T/1000},CREDS)])
        r=run_once(self.db,lambda:next(vals),lambda _:FakeClient([]),now=NOW)
        self.assertEqual(r['published'],0);self.assertEqual(r['inventory_issues'],1)
        self.assertTrue(self.db.exists())
    def test_empty_inventory_no_requests(self):
        r=run_once(self.db,lambda:({},None),lambda _:self.fail('unexpected client'),now=NOW)
        self.assertEqual(r['requests'],0)
    def test_inventory_registry_stopped_and_archive(self):
        base=self.root/'bots';base.mkdir();systemd=self.root/'units';systemd.mkdir()
        registry=self.root/'registry'; entries={}
        for name,inst,ack,retired in [('stopped',MMT,{},False),('old',MMT,{'phase':'PANIC_DONE'},False),('retired',ETH,{},True)]:
            root=base/name;root.mkdir();(root/'state').mkdir();(root/'state/control_ack.json').write_text(json.dumps(ack))
            (root/'config.json').write_text(json.dumps(dict(instrument=inst,created_at=T/1000)))
            (root/'.env').write_text('\n'.join(k+'='+v for k,v in CREDS.items()))
            service='okx-grid-'+name;(systemd/(service+'.service')).touch()
            entries[name]=dict(path=str(root),service=service,retired=retired)
        registry.write_text(json.dumps(entries));bots,creds=inventory(base,registry,systemd)
        self.assertEqual(set(bots),{MMT});self.assertEqual(creds,CREDS)
        (base/'old/state/control_ack.json').write_text('{}')
        bots,creds=inventory(base,registry,systemd)
        self.assertEqual(bots,{})
        self.assertIn(MMT,bots.blocked)
        self.assertEqual(creds,CREDS)
    def test_client_signing_and_pacing(self):
        calls=[];clock=[0.0]
        class Conn:
            def request(self,method,path,headers):calls.append((method,path,headers))
            def getresponse(self):return self
            status=200
            def read(self,n):return b'{"code":"0","data":[]}'
            def close(self):pass
        c=BillsClient(CREDS,clock=lambda:clock[0],sleep=lambda d:clock.__setitem__(0,clock[0]+d),connection=Conn)
        c.page(RECENT,T-1,T+1);c.page(ARCHIVE,T-1,T+1,'42')
        self.assertEqual(clock[0],1);self.assertEqual(c.requests,2)
        for method,path,headers in calls:
            self.assertEqual(method,'GET');q=parse_qs(urlsplit(path).query);self.assertEqual(q['end'],[str(T)]);self.assertNotIn('instId',q)
            import hmac,hashlib,base64
            self.assertEqual(headers['OK-ACCESS-SIGN'],base64.b64encode(hmac.new(CREDS['OKX_API_SECRET'].encode(),(headers['OK-ACCESS-TIMESTAMP']+'GET'+path).encode(),hashlib.sha256).digest()).decode())
        with self.assertRaisesRegex(LedgerError,'ENDPOINT_NOT_ALLOWED'):c.page('/api/v5/trade/order',T-1,T+1)
    def test_reconcile_watermark_persisted(self):
        self.run_fake([],mode='reconcile');self.assertEqual(read_state(self.db)[2],NOW)
    def test_daily_reconcile_full_seven_day_window(self):
        self.run_fake([],mode='reconcile');plan,_=plan_windows(self.bots,[],None,None,NOW,'reconcile')
        self.assertEqual(min(p[1] for p in plan),END-7*DAY)
        self.assertEqual(max(p[2] for p in plan),END)
    def test_missing_prior_record_preserves_snapshot(self):
        self.run_fake([bill(1)],mode='backfill');before=read_snapshot(self.db,MMT,self.bots[MMT])
        r,_=self.run_fake([bill(2)],now=NOW+HOUR)
        self.assertEqual(r['missing'],1)
        after=read_snapshot(self.db,MMT,self.bots[MMT])
        self.assertEqual(after['amount'],'-0.0034545');self.assertEqual(after['synced_ms'],NOW+HOUR)
        self.assertFalse(after['complete'])
    def test_http429_no_retry(self):
        class Conn:
            status=429
            def request(self,*a,**kw):pass
            def getresponse(self):return self
            def close(self):pass
        c=BillsClient(CREDS,connection=Conn)
        with self.assertRaisesRegex(LedgerError,'HTTP_429'):c.page(RECENT,T-1,T+1)
        self.assertEqual(c.requests,1)

if __name__=='__main__': unittest.main()
