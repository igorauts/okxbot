"""OFFLINE safety monitor P0 recovery and M4 tests; no Telegram/systemd/OKX."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import okx_telegram_journal_stream_v4 as stream
import okx_telegram_io_probe as io
import okx_reader_diagnostics as diag
import okx_telegram_phases_v4 as phases


class RecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.orig = (stream._ROWS, stream._STATE, stream._WANT, stream._THREAD,
                     stream._SEED_CURSOR, stream._ACKED_CURSOR, stream._ACKED_AT,
                     stream._INITIALIZED,stream._FORCE_TAIL)
        from collections import deque
        stream._ROWS=deque()
        stream._STATE=dict(self.orig[1]);stream._STATE.update(running=True,
                         overflow=0,gap_id='',gap_count=0,recovering=False,
                         last_error='',started_at=time.time(),last_line_at=time.time())
        stream._WANT=('near0721',)
        stream._THREAD=type('Alive',(),{'is_alive':lambda self:True})()
        stream._ACKED_CURSOR=None;stream._ACKED_AT=0.;stream._SEED_CURSOR=None
        stream._INITIALIZED=True;stream._FORCE_TAIL=False

    def tearDown(self):
        (stream._ROWS,stream._STATE,stream._WANT,stream._THREAD,
         stream._SEED_CURSOR,stream._ACKED_CURSOR,stream._ACKED_AT,
         stream._INITIALIZED,stream._FORCE_TAIL) = self.orig

    def test_bad_row_is_gap_not_permanent_overflow_latch(self):
        with stream._LOCK:
            stream._STATE['overflow']=1
            stream._gap('invalid JSON or cursor')
        st={}
        with patch('okx_telegram_events.one') as send:
            self.assertIsNone(stream.drain(st,['near0721']))
            self.assertEqual(send.call_count,1)
            # Recovery does not erase historical error counters.
            stream._STATE['recovering']=False
            stream._STATE['running']=True
            stream._STATE['started_at']=time.time()
            self.assertEqual(stream.drain(st,['near0721']),[])
            self.assertEqual(stream._STATE['overflow'],1)
            self.assertEqual(send.call_count,1)
            self.assertTrue(stream._FORCE_TAIL)

    def test_stale_persisted_cursor_forces_tail_and_gap_once(self):
        stream._INITIALIZED=False
        st={'buy_zero_stream_cursor_v4':'old',
            'buy_zero_stream_cursor_at_v4':time.time()-7200}
        with patch('okx_telegram_events.one') as send:
            self.assertIsNone(stream.drain(st,['near0721']))
            self.assertTrue(stream._FORCE_TAIL)
            self.assertNotIn('buy_zero_stream_cursor_v4',st)
            self.assertEqual(send.call_count,1)

    def test_recent_cursor_allowed(self):
        stream._INITIALIZED=False
        st={'buy_zero_stream_cursor_v4':'recent',
            'buy_zero_stream_cursor_at_v4':time.time()-3}
        with patch('okx_telegram_events.one') as send:
            self.assertEqual(stream.drain(st,['near0721']),[])
            self.assertEqual(stream._SEED_CURSOR,'recent')
            self.assertFalse(stream._FORCE_TAIL)
            send.assert_not_called()

    def test_real_follower_invalid_line_then_recovers_to_fresh_rows(self):
        import threading
        import os
        initial_stop=stream._STOP
        stream._STOP=threading.Event()
        first=threading.Event()
        second=threading.Event()
        proc_list=[]
        class FakeProcess:
            def __init__(self, content, hold):
                self.pid=123456
                r,w=os.pipe()
                self.stdout=os.fdopen(r,'rb',buffering=0)
                os.write(w,content)
                self.writer=w if hold else None
                if not hold:os.close(w)
                self.terminated=False
            def poll(self):return 0 if self.terminated else None
            def terminate(self):self.terminated=True
            def kill(self):self.terminated=True
            def wait(self,timeout=None):
                if self.writer is not None:
                    os.close(self.writer)
                    self.writer=None
                self.stdout.close()
                return 0
        def fake_popen(*a,**kw):
            if not proc_list:
                result=FakeProcess(b'invalid JSON\n',False)
                first.set()
            else:
                body=json.dumps({'__CURSOR':'fresh','_SYSTEMD_UNIT':'okx-grid-near0721.service',
                                 '__REALTIME_TIMESTAMP':str(int(time.time()*1e6)),
                                 'MESSAGE':'BUY 0/1 | РЕЖИМ=RUN | гриды 0/1'}).encode()+b'\n'
                result=FakeProcess(body,True)
                second.set()
            proc_list.append(result)
            return result
        try:
            with patch.object(stream.subprocess,'Popen',side_effect=fake_popen), \
                 patch('okx_telegram_events.one') as sent:
                thread=threading.Thread(target=stream._loop,daemon=True)
                thread.start()
                self.assertTrue(first.wait(1))
                self.assertTrue(second.wait(5))
                deadline=time.monotonic()+1
                while not stream._ROWS and time.monotonic()<deadline:
                    time.sleep(.02)
                st={}
                rows=stream.drain(st,['near0721'])
                self.assertEqual([r['__CURSOR'] for r in rows],['fresh'])
                self.assertEqual(stream._STATE['parse_errors']>=1,True)
                self.assertEqual(sent.call_count,1)
                self.assertTrue(stream.ack('fresh'))
        finally:
            stream._STOP.set()
            thread.join(timeout=3)
            stream._STOP=initial_stop

    def test_queue_overflow_discards_only_with_gap(self):
        with stream._LOCK:
            for i in range(stream._MAX):
                stream._ROWS.append({'__CURSOR':str(i)})
            stream._STATE['overflow']+=1
            stream._gap('queue overflow')
            self.assertEqual(len(stream._ROWS),0)
            self.assertEqual(stream._STATE['gap_count'],1)
            self.assertTrue(stream._STATE['recovering'])

    def test_repeated_failure_sends_one_gap_warning_until_fresh_ack(self):
        st={}
        with patch('okx_telegram_events.one') as sent:
            with stream._LOCK:stream._gap('bad row')
            old=stream._STATE['gap_id']
            stream.drain(st,['near0721'])
            with stream._LOCK:stream._gap('repeat bad row')
            stream.drain(st,['near0721'])
            self.assertEqual(stream._STATE['gap_id'],old)
            self.assertEqual(sent.call_count,1)
            stream._ROWS.append({'__CURSOR':'new'})
            self.assertTrue(stream.ack('new'))
            with stream._LOCK:stream._gap('second independent gap')
            self.assertNotEqual(stream._STATE['gap_id'],old)
            stream.drain(st,['near0721'])
            self.assertEqual(sent.call_count,2)

    def test_ack_absent_is_not_a_silent_success(self):
        self.assertFalse(stream.ack('missing'))
        self.assertTrue(stream._STATE['recovering'])
        self.assertTrue(stream._STATE['gap_id'])

    def test_ack_present(self):
        stream._ROWS.extend([{'__CURSOR':'a'},{'__CURSOR':'b'}])
        self.assertTrue(stream.ack('b'))
        self.assertEqual(len(stream._ROWS),0)
        self.assertGreater(stream._ACKED_AT,0)


class IOProbeContracts(unittest.TestCase):
    def test_missing_directory_does_not_block_monitor(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'missing'
            with patch.object(io,'_OUT',target),patch.object(io,'_INSTALLED',False),patch.object(io,'_advance_if_needed'):
                io.install()
                self.assertTrue(target.is_dir())
                self.assertEqual(target.stat().st_mode&0o777,0o700)
                io.subprocess.run=io._PREV_RUN
                io._INSTALLED=False

    def test_invalid_directory_fails_open(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'file'
            target.write_text('not a directory')
            with patch.object(io,'_OUT',target),patch.object(io,'_INSTALLED',False),patch.object(io,'_advance_if_needed'):
                io.install()  # must not raise or monkeypatch subprocess
                self.assertFalse(io._INSTALLED)


class IncrementalReaderContracts(unittest.TestCase):
    def test_append_and_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'latency.jsonl'
            now=time.time()
            def row(sec,phase,**extra):
                from datetime import datetime,timezone
                return json.dumps(dict(ts=datetime.fromtimestamp(sec,timezone.utc).isoformat(),phase=phase,**extra))+'\n'
            p.write_text(row(now-40,'position_reader_ipc',outcome='ok'))
            a=diag._latency_summary_incremental(p,now=now)
            self.assertEqual((a['ipc_ok'],a['direct']),(1,0))
            with p.open('a') as f:
                f.write(row(now-5,'positions_gate',route=diag.POSITIONS_ROUTE))
            b=diag._latency_summary_incremental(p,now=now)
            self.assertEqual((b['ipc_ok'],b['direct']),(1,1))
            p.unlink()
            p.write_text(row(now,'position_reader_ipc',outcome='failed'))
            c=diag._latency_summary_incremental(p,now=now)
            self.assertEqual((c['ipc_ok'],c['ipc_fail'],c['direct']),(0,1,0))

    def test_aged_out(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'latency.jsonl';now=time.time()
            from datetime import datetime,timezone
            p.write_text(json.dumps({'ts':datetime.fromtimestamp(now-3599,timezone.utc).isoformat(),
                                     'phase':'position_reader_ipc','outcome':'ok'})+'\n')
            a=diag._latency_summary_incremental(p,now=now)
            self.assertEqual(a['ipc_ok'],1)
            b=diag._latency_summary_incremental(p,now=now+3)
            self.assertEqual(b['ipc_ok'],0)


class PhaseContracts(unittest.TestCase):
    def test_measure_exceptions_and_cpu_record(self):
        def boom(): raise ValueError('oops')
        with self.assertRaises(ValueError): phases.measure('test_exception',boom)
        self.assertEqual(phases._BUCKETS['test_exception'][0],1)

if __name__=='__main__':unittest.main()

class HTTPSKeepAliveContracts(unittest.TestCase):
    """Compile the isolated transport class; the monitor's live path is absent offline."""
    def _class(self):
        import ast, http.client, ssl
        from urllib.request import Request, urlopen, getproxies
        source=Path(__file__).resolve().parents[1].joinpath('okx_telegram_monitor.py').read_text()
        tree=ast.parse(source)
        nodes=[n for n in tree.body if isinstance(n,ast.ClassDef) and
               n.name in ('Telegram','TelegramPermanentError')]
        space=dict(json=json,http=http,ssl=ssl,Request=Request,urlopen=urlopen,
                   getproxies=lambda:{})
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'transport_offline','exec'),space)
        return space['Telegram'],space['TelegramPermanentError']

    def test_connection_reused_and_response_consumed(self):
        Telegram,_=self._class()
        requests=[];connections=[]
        class Resp:
            status=200;will_close=False
            def read(self):return b'{"ok":true,"result":[]}'
        class Conn:
            def __init__(self,*a,**kw):connections.append(self)
            def request(self,*a,**kw):requests.append((a,kw))
            def getresponse(self):return Resp()
            def close(self):pass
        import http.client
        with patch.object(http.client,'HTTPSConnection',Conn):
            api=Telegram('1234:abcdeABCDE12345','1234')
            api.call('getUpdates',{'offset':1})
            api.call('getUpdates',{'offset':2})
            self.assertEqual(len(connections),1)
            self.assertEqual(len(requests),2)

    def test_500_does_not_retry_send_and_closes_connection(self):
        Telegram,_=self._class()
        requests=[];closed=[]
        class Resp:
            status=500;will_close=False
            def read(self):return b'{}'
        class Conn:
            def __init__(self,*a,**kw):pass
            def request(self,*a,**kw):requests.append(a)
            def getresponse(self):return Resp()
            def close(self):closed.append(True)
        import http.client
        with patch.object(http.client,'HTTPSConnection',Conn):
            api=Telegram('1234:abcdeABCDE12345','1234')
            with self.assertRaises(RuntimeError):api.call('sendMessage',{'text':'x'})
            self.assertEqual(len(requests),1)
            self.assertTrue(closed)
            self.assertIsNone(api._http)
