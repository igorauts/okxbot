"""Offline candidate: coordinate concurrent per-order fill fetches in one bot.

Does not change accounting, return fills, assume completeness, or perform network
I/O by itself. Callers must re-check durable SQLite quantities after waiting.
"""
import threading


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.error = None
        self.result = None


class FillSingleFlight:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = {}
        self.leaders = 0
        self.coalesced = 0

    def run(self, order_id, fetch):
        key = str(order_id)
        with self._lock:
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                flight = self._inflight[key] = _Flight()
                self.leaders += 1
            else:
                self.coalesced += 1
        if not leader:
            if not flight.done.wait(timeout=60):
                raise TimeoutError('fills-history singleflight fetch stuck')
            if flight.error is not None:
                raise flight.error
            return flight.result
        try:
            flight.result = fetch()
            return flight.result
        except BaseException as error:
            flight.error = error
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                flight.done.set()
