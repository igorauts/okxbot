#!/usr/bin/env python3
"""READ-ONLY, explicitly launched candidate Account Position Reader.

Not installed/enabled by any package script. Requires manual operator setup,
a single-account READ-authorized OKX credential file and private Unix dir.
All exchange calls are GET. No order/position POST/DELETE methods exist.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import ssl
import time
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from okx_position_reader_core import Registration, SharedReaderCore
from okx_position_reader_ipc import ReaderIPCServer
from okx_positions_priority import PriorityPositionsFIFO
from okx_positions_fifo import AccountPositionsFIFO

class CancellableLegacyFIFO(AccountPositionsFIFO):
    """No new quota and no priority sort; checks liveness before each visit."""
    def __init__(self, path):
        super().__init__(path)
        self._thread = threading.local()
    def _visit(self, ticket, born, *, cancel=False):
        if not cancel:
            fn=getattr(self._thread,'need',None)
            if fn is not None: fn()  # raises if all IPC waiters timed out
        return super()._visit(ticket,born,cancel=cancel)
    def take_cancellable(self, supplier):
        self._thread.need=supplier
        try:
            return self.take()
        finally:
            self._thread.need=None

class SharedOriginalQuota:
    """Exactly ONE account quota file and FIFO queue in either scheduling mode.

    Mixed 0.7.33/0.7.34 fleet: the Reader must NOT sit behind all legacy
    unlabeled tickets. Standard FIFO is the only safe fairness contract when
    old requests can be PANIC or routine and cannot be distinguished.
    Priority mode is opt-in only AFTER all trading engines on this account
    have been verified as priority-aware. Neither mode creates a second quota.
    """
    def __init__(self,path, *, scheduling='mixed-fifo'):
        if scheduling not in ('mixed-fifo','priority'):
            raise ValueError('Invalid Reader FIFO scheduling mode')
        self.scheduling=scheduling
        self.gate=(CancellableLegacyFIFO(path) if scheduling=='mixed-fifo'
                   else PriorityPositionsFIFO(path))
    async def take(self, priority_supplier):
        if self.scheduling=='mixed-fifo':
            # Do not promote Reader above a preexisting legacy PANIC ticket;
            # equally, do not demote it behind every old routine request.
            # Continuously check whether requests are still pending so an
            # abandoned Reader does not consume a quota permit later.
            await asyncio.to_thread(self.gate.take_cancellable,priority_supplier)
        else:
            await asyncio.to_thread(self.gate.take_prioritized,priority_supplier)

class ReadOnlyOKXProvider:
    """GET-only, per-thread TLS keep-alive. A failed request is NOT retried
    inside provider: a new wire attempt would require another quota permit.
    """
    HTTP_TIMEOUT_SEC = 4.5
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, key, secret, passphrase):
        if not all(isinstance(s,str) and s for s in (key,secret,passphrase)):
            raise ValueError('Missing account credentials')
        self._key,self._secret,self._passphrase=key,secret,passphrase
        self._thread = threading.local()

    def _connection(self):
        conn = getattr(self._thread, 'connection', None)
        if conn is None:
            conn = http.client.HTTPSConnection(
                'www.okx.com', timeout=self.HTTP_TIMEOUT_SEC,
                context=ssl.create_default_context())
            self._thread.connection = conn
        return conn

    def _close(self):
        conn = getattr(self._thread, 'connection', None)
        self._thread.connection = None
        if conn is not None:
            try: conn.close()
            except OSError: pass

    def _sync_get_path(self,path):
        ts=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        sign=base64.b64encode(hmac.new(self._secret.encode(),
                  (ts+'GET'+path).encode(),hashlib.sha256).digest()).decode()
        headers={'User-Agent':'okxbot-reader/offline-candidate','Content-Type':'application/json',
                 'OK-ACCESS-KEY':self._key,'OK-ACCESS-SIGN':sign,
                 'OK-ACCESS-TIMESTAMP':ts,'OK-ACCESS-PASSPHRASE':self._passphrase}
        conn=self._connection()
        try:
            conn.request('GET',path,headers=headers)
            res=conn.getresponse()
            raw=res.read(self.MAX_RESPONSE_BYTES+1)
            if res.will_close: self._close()
            if len(raw)>self.MAX_RESPONSE_BYTES or res.status!=200:
                self._close()
                raise RuntimeError('Reader GET failed or oversized')
            obj=json.loads(raw)
            if not isinstance(obj,dict) or obj.get('code')!='0' or not isinstance(obj.get('data'),list):
                raise RuntimeError('Reader GET rejected by exchange')
            return obj
        except (OSError, TimeoutError, ConnectionError,
                http.client.HTTPException, ssl.SSLError):
            self._close()
            raise

    async def fetch(self,instruments):
        return await asyncio.to_thread(self._sync_get,instruments)

    def _sync_get(self,instruments):
        if not 1<=len(instruments)<=10:
            raise ValueError('Invalid instrument count')
        # Request the SWAP scope explicitly; never include inverse/USDC.
        path='/api/v5/account/positions?'+urllib.parse.urlencode({
            'instType':'SWAP', 'instId':','.join(instruments)})
        return self._sync_get_path(path)

    async def fetch_account_uid(self):
        """One signed GET on service startup; fail if UID not proven."""
        data=await asyncio.to_thread(self._sync_get_path,'/api/v5/account/config')
        records=data['data']
        if len(records)!=1 or not isinstance(records[0],dict):
            raise RuntimeError('Ambiguous OKX account config')
        uid=records[0].get('uid')
        if not isinstance(uid,str) or not uid.strip() or len(uid)>64:
            raise RuntimeError('Unverified OKX account UID')
        return uid

def read_credentials(path):
    p=Path(path)
    if not p.is_file() or p.is_symlink() or p.stat().st_mode&0o077 or p.stat().st_uid != os.geteuid():
        raise RuntimeError('Credential file requires private regular 0600 permissions')
    lines={}
    for line in p.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        k,sep,v=line.partition('=')
        if sep and k.strip() in ('OKX_API_KEY','OKX_API_SECRET','OKX_API_PASSPHRASE'):
            if k.strip() in lines:raise RuntimeError('Duplicate credential field')
            lines[k.strip()]=v.strip().strip('"').strip("'")
    return [lines[k] for k in ('OKX_API_KEY','OKX_API_SECRET','OKX_API_PASSPHRASE')]

def _bot_engine_process_absent(root: Path) -> bool:
    """Fail closed on any uncertain /proc scan of the terminal bot.

    Only used to supplement a terminal ACK + missing/failed systemd unit.
    Never regards a missing unit ALONE as proof of an archived bot.
    """
    root = Path(root).resolve()
    try:
        for p in Path('/proc').iterdir():
            if not p.name.isdecimal():
                continue
            try:
                data=(p/'cmdline').read_bytes()
                if b'okx_bot.py' not in data:
                    continue
                argv=[x.decode('utf-8','surrogateescape') for x in data.split(b'\0') if x]
                direct=any(Path(x).is_absolute() and
                           Path(x).resolve()==root/'okx_bot.py' for x in argv)
                try:
                    cwd=(p/'cwd').resolve(strict=True)
                except FileNotFoundError:
                    continue  # raced with an exited process
                if direct or cwd==root:
                    return False
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (PermissionError, OSError, UnicodeError):
                return False
    except (OSError, PermissionError):
        return False
    return True

def _terminal_unit_inactive(service: str, root: Path = None) -> bool:
    """Archive proof must include a known, stopped systemd unit (PID 0).

    `systemctl is-active` returning nonzero alone is insufficient: missing,
    failed and unqueryable units can return nonzero too. Uncertainty consumes
    an account bot slot instead of hiding a potentially live instrument.
    """
    if not isinstance(service, str) or not service.startswith('okx-grid-'):
        return False
    try:
        result = subprocess.run(
            ['systemctl', 'show', service + '.service', '--no-pager',
             '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID'],
            capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    values = {}
    for line in result.stdout.splitlines():
        if '=' not in line:
            return False
        key, value = line.split('=', 1)
        if key in values:
            return False
        values[key] = value
    if values.get('MainPID') != '0':
        return False
    if values.get('LoadState') == 'loaded' and values.get('ActiveState') == 'inactive':
        return True
    # Only a separately validated terminal ACK may reach here. Missing / failed
    # units must also have a proven-absent bot process; uncertainty keeps slot.
    if (root is not None and values.get('LoadState') in ('not-found','loaded')
            and values.get('ActiveState') in ('inactive','failed')):
        return _bot_engine_process_absent(root)
    return False


def load_registry(path):
    """Read-only classification; ACK+inactive is truth, `retired` is a hint.

    A stale `retired=False` must not revive a durable PANIC_DONE instance.
    Conversely, stopped RUN/SOFT instances still consume slots. Unknown state
    never creates an implicit archive and never mutates the registry.
    """
    root = Path(path).resolve().parent
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('Invalid registry')
    records = []
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError('Malformed registry entry')
        d = Path(entry['path']).resolve()
        if d.parent != root:
            raise ValueError('Outside single-account base')
        cfg = json.loads((d / 'config.json').read_text(encoding='utf-8'))
        ack_path = d / 'state/control_ack.json'
        retired = entry.get('retired') is True
        ack = None
        if ack_path.exists() or ack_path.is_symlink():
            if ack_path.is_symlink() or not ack_path.is_file():
                raise RuntimeError(f'Bot {name!r} has unsafe control_ack.json at {ack_path}')
            try:
                ack = json.loads(ack_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f'Bot {name!r} has unreadable control_ack.json at {ack_path}') from exc
            if not isinstance(ack, dict):
                raise RuntimeError(f'Bot {name!r} has invalid control_ack.json at {ack_path}')
        elif retired:
            raise RuntimeError(
                f'Retired bot {name!r} missing control_ack.json at {ack_path}; '
                'Reader startup refused, existing bots may use shared-gate direct GET')
        terminal = (ack is not None and ack.get('phase') in ('PANIC_DONE', 'RETIRED'))
        archived = bool(terminal and _terminal_unit_inactive(entry.get('service'), d))
        records.append(Registration(name, cfg['instrument'], archived))
    return records

async def main():
    p=argparse.ArgumentParser()
    p.add_argument('--registry',required=True)
    p.add_argument('--credentials-file',required=True)
    p.add_argument('--shared-gate',required=True)
    p.add_argument('--socket',required=True)
    p.add_argument('--read-only-ack',required=True,choices=['POSITIONS_GET_ONLY'])
    p.add_argument('--quota-scheduling', choices=['mixed-fifo','priority'],
                   default='mixed-fifo',
                   help='mixed-fifo is mandatory during rolling upgrade with old engines')
    p.add_argument('--priority-ready-ack',choices=['ALL_BOTS_PRIORITY_AWARE'])
    args=p.parse_args()
    if (args.quota_scheduling=='priority' and
            args.priority_ready_ack!='ALL_BOTS_PRIORITY_AWARE'):
        p.error('Priority mode requires verified full-fleet migration and explicit ack')
    registrations=load_registry(args.registry)
    provider = ReadOnlyOKXProvider(*read_credentials(args.credentials_file))
    account_uid = await provider.fetch_account_uid()
    core=SharedReaderCore(registrations, provider,
                          SharedOriginalQuota(args.shared_gate,
                                              scheduling=args.quota_scheduling),
                          account_uid=account_uid, get_timeout_sec=4.5)
    service=ReaderIPCServer(core,Path(args.socket),
                            registry_loader=lambda: load_registry(args.registry))
    await service.start()
    try:
        await asyncio.Event().wait()
    finally:
        await service.close()

if __name__=='__main__':
    asyncio.run(main())
