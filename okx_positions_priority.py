"""Offline candidate priority-aware extension of the original account FIFO.

Shared schema-1 queue and SAME quota/lock files. Legacy FIFO clients safely read
extra fields and follow the reordered queue. Never creates a second quota.
No claim of strict emergency preemption when a legacy client already holds quota.
"""
import math
import threading
from okx_positions_fifo import AccountPositionsFIFO, GateUnavailable

EMERGENCY, EXIT, NORMAL, BACKGROUND = 0, 1, 2, 3

class PriorityPositionsFIFO(AccountPositionsFIFO):
    FAIRNESS_AGE_SEC = 3.0

    def __init__(self, shared_path, reporter=None):
        super().__init__(shared_path, reporter=reporter)
        self._thread_priority = threading.local()

    @staticmethod
    def _valid_priority(value):
        if type(value) is not int or value not in (0, 1, 2, 3):
            raise GateUnavailable('Invalid FIFO priority; fail closed')
        return value

    def _current_priority(self):
        fn = getattr(self._thread_priority, 'priority_supplier', None)
        priority = fn() if fn is not None else NORMAL
        return self._valid_priority(priority)

    def _entry_extension(self):
        return {'priority': self._current_priority()}

    def _sort_queue(self, queue, now):
        # Legacy engines do NOT label emergency reads. Thus an unlabeled
        # legacy ticket may be a PANIC/HARD GET: never classify it as NORMAL
        # and preempt it with a new EXIT. During mixed fleet, all unlabeled
        # tickets conservatively rank EMERGENCY until the fleet is upgraded.
        requested = self._current_priority()
        this_ticket = getattr(self._thread_priority, 'ticket', None)
        updated = [dict(row) for row in queue]
        for row in updated:
            raw = row.get('priority', EMERGENCY)
            self._valid_priority(raw)
            if row.get('id') == this_ticket and raw != requested:
                row['priority'] = requested  # dynamic promotion while waiting
        def key(row):
            age = max(0.0, now - row['created'])
            rank = row.get('priority', EMERGENCY)
            # EMERGENCY is always highest. Other clients age up to EXIT after
            # 3s, preventing starvation under a continuous urgent workload.
            if rank != EMERGENCY and age >= self.FAIRNESS_AGE_SEC:
                rank = EXIT
            return rank, row['created'], row['id']
        return sorted(updated, key=key)

    def _visit(self, ticket, born, *, cancel=False):
        self._thread_priority.ticket = ticket
        try:
            return super()._visit(ticket, born, cancel=cancel)
        finally:
            self._thread_priority.ticket = None

    def take_prioritized(self, priority_supplier):
        if not callable(priority_supplier):
            raise GateUnavailable('Priority supplier required')
        self._thread_priority.priority_supplier = priority_supplier
        try:
            return self.take()
        finally:
            self._thread_priority.priority_supplier = None
