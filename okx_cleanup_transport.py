#!/usr/bin/env python3
"""OFFLINE CANDIDATE: single-engine, one-at-a-time nonblocking CANCEL POST transport.

No code runs at import beyond declarations. Transport cannot place orders,
write state or decide whether an order should be canceled. All order ownership,
price/quantity validation, durable intents and ACK processing remain in the
trading thread. Thread only sends one previously authorized CANCEL POST.

An HTTP call already in flight cannot be unsent. A new private fill must
prevent *new* discretionary cancels; it cannot undo an already-sent POST.
No extra trading process or account identity is created.
"""
from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable, Optional


@dataclass(frozen=True)
class CancelCompletion:
    kind: str
    ord_id: str
    token: str
    reply: object
    error: Optional[Exception]
    duration_s: float
    sent: bool


class SingleOwnerCancelTransport:
    """Bounded transport: one request outstanding, at most one worker thread.

    submit only returns True when `durable_prepare` succeeded and the
    request was queued. It does not claim OKX accepted or finalized CANCEL.

    Caller may consume a completion only on the owner trading thread. The
    trading thread must persist rejection and then reconcile by ordId. If the
    outcome is uncertain, the durable CANCELING record remains fail-closed.
    """
    def __init__(self, send_cancel: Callable[[str], object],
                 urgent_pending: Callable[[], bool],
                 wake_owner: Callable[[], None],
                 *, critical_pending: Optional[Callable[[], bool]] = None,
                 interval_s: float = 0.1,
                 monotonic: Callable[[], float] = time.monotonic):
        if not (0.1 <= interval_s <= 10.0):
            raise ValueError('Cancellation spacing below 100 ms or invalid')
        self._send_cancel = send_cancel
        self._urgent_pending = urgent_pending
        self._critical_pending = critical_pending or urgent_pending
        self._wake_owner = wake_owner
        self._interval_s = interval_s
        self._clock = monotonic
        self._queue = queue.Queue(maxsize=1)
        self._completions = queue.Queue(maxsize=1)
        self._mutex = threading.Lock()
        self._busy = False
        self._last_reserved_at = None
        self._last_sent_at = None
        self._worker = threading.Thread(target=self._worker_loop,
                                        name='okx-elective-cancel-transport',
                                        daemon=True)
        self._worker.start()

    def submit(self, *, kind: str, ord_id: str, token: str,
               durable_prepare: Callable[[], None], emergency: bool = False) -> bool:
        if kind not in ('BUY', 'TP') or not isinstance(ord_id, str) or not ord_id:
            raise ValueError('Invalid cancellation identity')
        if not isinstance(token, str) or not token:
            raise ValueError('Missing cancellation token')
        # Important: preemption check BEFORE durable state is mutated.
        preempt = self._critical_pending if emergency else self._urgent_pending
        if preempt():
            return False
        with self._mutex:
            now=self._clock()
            if self._busy or (self._last_reserved_at is not None and
                now-self._last_reserved_at < self._interval_s):
                return False
            if preempt():
                return False
            self._busy=True
            self._last_reserved_at=now
        # This runs on MAIN thread and must fsync the complete CANCELING
        # intent BEFORE the worker is permitted to touch REST.
        try:
            durable_prepare()
        except BaseException:
            # A failed durable save cannot cause a network CANCEL.
            with self._mutex:
                self._busy=False
            raise
        # Exactly one prepared request, with no retry and no loss.
        # Worker consumes one item at a time; caller has reserved _busy.
        self._queue.put_nowait((kind,ord_id,token,emergency))
        return True

    def _worker_loop(self):
        while True:
            kind,ord_id,token,emergency=self._queue.get()
            start=self._clock()
            reply=None;error=None;sent=False
            try:
                # Recheck immediately before entering REST. If a private fill
                # or emergency command arrived after owner-side durable save,
                # NO request is sent. Owner can safely reverse only the local
                # unsubmitted intent, then revalidate via fresh REST.
                preempt = self._critical_pending if emergency else self._urgent_pending
                # The 100ms spacing applies to ACTUAL network send starts,
                # not just the time durable intents were reserved. A preceding
                # POST may have spent seconds waiting in the REST governor.
                # This sleep is in the worker only; owner never waits here.
                with self._mutex:
                    gap = (0.0 if self._last_sent_at is None else
                           self._interval_s - (self._clock()-self._last_sent_at))
                if gap > 0:
                    time.sleep(gap)
                # Check urgent intent again AFTER the spacing delay, just
                # before entering the REST client.
                if not preempt():
                    with self._mutex:
                        self._last_sent_at = self._clock()
                    sent=True  # outcome becomes uncertain once POST starts
                    reply=self._send_cancel(ord_id)
            except Exception as exc:
                error=exc  # No silent retry of ambiguous POST.
            finally:
                elapsed=max(0.0,self._clock()-start)
                self._completions.put(CancelCompletion(
                    kind,ord_id,token,reply,error,elapsed,sent))
                # Wake immediately for owner-side ACK/uncertainty processing.
                self._wake_owner()

    def consume(self) -> Optional[CancelCompletion]:
        try:
            item=self._completions.get_nowait()
        except queue.Empty:
            return None
        with self._mutex:
            if not self._busy:
                raise RuntimeError('Completion without a reserved cancellation')
            self._busy=False
        return item

    @property
    def busy(self):
        with self._mutex:
            return self._busy
