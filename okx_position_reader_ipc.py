"""Unix-socket demo transport for the OFFLINE SharedReaderCore only.

This module has no main() and cannot initiate OKX exchange requests. A caller
must inject a fake provider and an offline quota into SharedReaderCore.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import time
from pathlib import Path

from okx_position_reader_core import Priority, ReaderError, SharedReaderCore

MAX_LINE = 4096
WIRE_SCHEMA = 1  # IPC protocol only, NOT a trading-engine/release version


class ReaderIPCServer:
    def __init__(self, core: SharedReaderCore, socket_path: Path):
        self.core = core
        self.socket_path = Path(socket_path)
        self.server = None

    async def start(self):
        if not isinstance(self.core.account_uid, str) or not self.core.account_uid:
            raise ReaderError("Reader identity unverified")
        # A protected pre-existing filesystem socket must never be replaced.
        if not self.socket_path.parent.is_dir() or self.socket_path.exists() or self.socket_path.is_symlink():
            raise ReaderError("Socket directory missing or socket path already in use")
        if self.socket_path.parent.stat().st_mode & 0o077:
            raise ReaderError("Socket directory must be private (0700)")
        await self.core.start()
        try:
            self.server = await asyncio.start_unix_server(
                self._handle, path=str(self.socket_path), limit=MAX_LINE + 1)
            os.chmod(self.socket_path, 0o600)
        except Exception:
            await self.core.close()
            raise

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await self.core.close()
        if self.socket_path.is_socket():
            self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # The private socket alone is not enough: verify same local UID.
            sock=writer.get_extra_info('socket')
            if not sock or not hasattr(socket,'SO_PEERCRED'):
                raise ReaderError('Unix peer authentication unavailable')
            pid, uid, gid=struct.unpack('3i',sock.getsockopt(
                socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
            if uid!=os.geteuid() or pid<=0:
                raise ReaderError('Unauthorized IPC peer')
            raw = await asyncio.wait_for(reader.readline(), 5.0)
            if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
                raise ReaderError("Oversized/incomplete IPC message")
            obj = json.loads(raw)
            if not isinstance(obj, dict) or obj.get('schema') != WIRE_SCHEMA:
                raise ReaderError("Unknown IPC protocol")
            if obj.get('op') == 'health':
                worker=self.core._worker_task
                alive=(worker is not None and not worker.done() and
                       not self.core._closed)
                reply={"ok":alive,"data":{
                    "reader_running":alive,
                    "fetch_failed":self.core.counters['fetch_failed'],
                    "quota_failed":self.core.counters['quota_failed'],
                    "quota_abandoned":self.core.counters.get('quota_abandoned',0),
                    "unused_quota_grants":self.core.counters.get('unused_quota_grants',0),
                    "pending":len(self.core._pending()),
                    "inflight_age_ms":(max(0,round((time.monotonic_ns()
                        - self.core._inflight_since_ns)/1_000_000,3))
                        if self.core._inflight_since_ns else 0),
                }}
            elif obj.get('op') == 'positions':
                requested = obj.get('priority')
                if type(requested) is not int:
                    raise ReaderError("Priority must be an integer")
                result = await self.core.request(
                    obj.get('instrument'), Priority(requested),
                    not_before_ns=obj.get('not_before_mono_ns'),
                    timeout_sec=4.8, trace_id=obj.get('trace_id', ''),
                    account_uid=obj.get('account_uid'))
                reply = {"ok": True, "data": result}
            else:
                raise ReaderError("Unknown IPC operation")
        except Exception as exc:
            reply = {"ok": False, "error": type(exc).__name__}
        try:
            writer.write((json.dumps(reply, separators=(',', ':')) + '\n').encode('utf-8'))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def ipc_request(socket_path: Path, instrument: str, priority: Priority,
                      *, not_before_ns: int | None = None, trace_id: str = "",
                      account_uid: str) -> dict:
    if not_before_ns is None:
        not_before_ns = __import__('time').monotonic_ns()
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)), 3.0)
    try:
        message = {
            "schema": WIRE_SCHEMA, "op": "positions", "instrument": instrument,
            "priority": int(priority), "not_before_mono_ns": not_before_ns,
            "trace_id": trace_id[:128], "account_uid": account_uid,
        }
        writer.write((json.dumps(message, separators=(',', ':'))+'\n').encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), 5.0)
        if not response or len(response) > 64*1024:
            raise ReaderError('IPC response missing/oversized')
        decoded = json.loads(response)
        if not isinstance(decoded, dict) or decoded.get('ok') is not True:
            raise ReaderError('IPC request rejected')
        return decoded['data']
    finally:
        writer.close()
        await writer.wait_closed()
