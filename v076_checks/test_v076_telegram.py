#!/usr/bin/env python3
"""No exchange/Telegram network. Tests production monitor event reader by AST."""
import ast, json, re, tempfile, time, unittest
from pathlib import Path

SRC=Path(__file__).with_name('okx_telegram_monitor.py').read_text()
TREE=ast.parse(SRC)

def symbol(name,env):
    node=next(n for n in TREE.body if isinstance(n,(ast.FunctionDef,ast.Assign))
              and ((isinstance(n,ast.FunctionDef) and n.name==name) or
                   (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets))))
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<exact monitor>','exec'),env)
    return env[name]

class TelegramMarketAlerts(unittest.TestCase):
    def test_market_events_are_all_included(self):
        env={}
        data=symbol('ALERT_EVENTS',env)
        for event in ('AGG_MARKET_INTENT','AGG_MARKET_ACK',
                      'AGG_MARKET_REJECTED','AGG_MARKET_TERMINAL'):
            self.assertIn(event,data)

    def test_two_market_sells_not_throttled_by_600_second_alert_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'events.jsonl'
            records=[
                {'event':'AGG_MARKET_TERMINAL','clOrdId':'a','ordId':'A',
                 'filled':'1','requested':'1','avgPx':'100','state':'FILLED'},
                {'event':'AGG_MARKET_TERMINAL','clOrdId':'b','ordId':'B',
                 'filled':'2','requested':'2','avgPx':'101','state':'FILLED'},
                {'event':'TP_RESTORE_REJECTED','severity':'error','code':'8'},
                {'event':'TP_RESTORE_REJECTED','severity':'error','code':'9'},
            ]
            f.write_text(''.join(json.dumps(r)+'\n' for r in records))
            sent=[]
            class Bot:
                def send(self,s):sent.append(s)
            env={'BOT_DIRS':{'ETH':str(Path(d))},'Path':Path,'json':json,
                 'ALERT_EVENTS':symbol('ALERT_EVENTS',{}),
                 'MARKET_ALERTS':frozenset(('AGG_MARKET_INTENT','AGG_MARKET_ACK',
                    'AGG_MARKET_REJECTED','AGG_MARKET_TERMINAL')),
                 'RATE_LIMIT_SECONDS':600,'time':time,'re':re,
                 'save_state':lambda st:None}
            Path(d,'logs').mkdir();f.replace(Path(d,'logs','bot04_events.jsonl'))
            symbol('format_alert',env)
            ingest=symbol('ingest_events',env)
            st={'offsets':{'ETH':0},'last_alerts':{}}
            ingest(Bot(),st)
            self.assertEqual(len(sent),3)  # two distinct markets, one throttled conventional alert
            self.assertIn('avgPx: 100',sent[0]);self.assertIn('avgPx: 101',sent[1])
            self.assertEqual(st['offsets']['ETH'],Path(d,'logs','bot04_events.jsonl').stat().st_size)
            ingest(Bot(),st)
            self.assertEqual(len(sent),3)  # persisted cursor prevents duplicates

if __name__=='__main__':unittest.main()
