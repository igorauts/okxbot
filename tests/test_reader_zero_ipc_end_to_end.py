"""Reader authoritative-zero end-to-end tests; fake provider, no OKX network."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from okx_position_reader_core import Registration, SharedReaderCore
from okx_position_reader_ipc import ReaderIPCServer
from okx_position_reader_client import read_positions, PositionReaderUnavailable, PositionReaderEmpty

A = 'INIT-USDT-SWAP'
B = 'BTC-USDT-SWAP'

class Quota:
    async def take(self, priority_supplier):
        priority_supplier()

class Provider:
    def __init__(self, rows, *, full=True, wrong_scope=False):
        self.rows=rows
        self.full=full
        self.wrong_scope=wrong_scope
        self.calls=0
    async def fetch(self, instruments):
        self.calls+=1
        data={'code':'0', 'data':self.rows}
        if self.full:
            data['_reader_transport_scope']={
                'endpoint':'/api/v5/account/positions', 'instType':'SWAP',
                'instIds': list(reversed(instruments)) if self.wrong_scope else list(instruments),
                'complete': True}
        return data

class RealIPC(unittest.TestCase):
    def exchange(self, *, rows, full=True, wrong_scope=False, uid='account-verified', instrument=A,
                 expect_empty=False, expect_unavailable=False):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='reader-zero-ipc-') as folder:
                # TemporaryDirectory is mode 0700, not shared with any live socket.
                p=Path(folder)/'reader.sock'
                provider=Provider(rows,full=full,wrong_scope=wrong_scope)
                core=SharedReaderCore([Registration('init-test',A),Registration('btc-test',B)],
                    provider,Quota(),account_uid='account-verified')
                server=ReaderIPCServer(core,p)
                await server.start()
                try:
                    def client():
                        return read_positions(str(p),instrument,account_uid=uid,trace_id='trace-1')
                    if expect_empty:
                        with self.assertRaises(PositionReaderEmpty):
                            await asyncio.to_thread(client)
                    elif expect_unavailable:
                        with self.assertRaises(PositionReaderUnavailable):
                            await asyncio.to_thread(client)
                    else:
                        result=await asyncio.to_thread(client)
                        self.assertIsInstance(result,list)
                        self.assertEqual(result, rows if rows and rows[0].get('instId') == instrument else [])
                    return provider.calls
                finally:
                    await server.close()
        return asyncio.run(scenario())

    def test_validated_empty_response_crosses_real_unix_ipc(self):
        self.assertEqual(self.exchange(rows=[]),1)
    def test_missing_transport_proof_rejected(self):
        self.exchange(rows=[],full=False,expect_empty=True)
    def test_wrong_exact_scope_rejected(self):
        self.exchange(rows=[],wrong_scope=True,expect_empty=True)
    def test_uid_mismatch_rejected_without_provider_fetch(self):
        self.assertEqual(self.exchange(rows=[],uid='wrong-account',expect_unavailable=True),0)
    def test_nonzero_net_position_is_retained(self):
        self.exchange(rows=[{'instId':A,'posSide':'net','pos':'3'}])
    def test_nonnet_row_rejected(self):
        self.exchange(rows=[{'instId':A,'posSide':'long','pos':'3'}],expect_unavailable=True)
    def test_unrelated_instrument_does_not_prove_zero(self):
        self.exchange(rows=[{'instId':'ETH-USDT-SWAP','posSide':'net','pos':'2'}],expect_empty=True)
    def test_duplicate_target_row_rejected(self):
        row={'instId':A,'posSide':'net','pos':'3'}
        self.exchange(rows=[row,row],expect_unavailable=True)
    def test_other_instrument_open_and_requested_one_zero(self):
        self.exchange(rows=[{'instId':B,'posSide':'net','pos':'1'}],instrument=A)

if __name__ == '__main__': unittest.main()
