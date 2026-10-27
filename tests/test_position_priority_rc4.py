"""Offline mixed-legacy priority queue/one quota tests. No API/systemd."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from okx_positions_fifo import AccountPositionsFIFO, GateUnavailable
from okx_positions_priority import PriorityPositionsFIFO, NORMAL, EXIT, EMERGENCY, BACKGROUND


class PriorityQueueContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'gate.json'
        self.old=AccountPositionsFIFO(path)
        self.new=PriorityPositionsFIFO(path)
        path.write_text(json.dumps({'schema':1,'calls':[]}))
        self.now=time.time()

    def seed(self, entries):
        self.new.queue_path.write_text(json.dumps({'schema':1,'waiters':[
            {'id':ticket,'pid':os.getpid(),'created':self.now-age,'seen':self.now,
             **({'priority':priority} if priority is not None else {})}
            for ticket,age,priority in entries]}))

    def test_unknown_legacy_ticket_cannot_be_preempted_even_by_new_emergency(self):
        # An old unlabeled ticket might be PANIC. Preserve its original place.
        self.seed([('old',.20,None)])
        self.new._thread_priority.priority_supplier=lambda:EMERGENCY
        try:
            granted,_=self.new._visit('urgent',None)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertFalse(granted)
        self.assertEqual([x['id'] for x in json.loads(self.new.queue_path.read_text())['waiters']],
                         ['old','urgent'])
        old_grant,_=self.old._visit('old',True)
        self.assertTrue(old_grant)
        self.new._thread_priority.priority_supplier=lambda:EMERGENCY
        try: granted,_=self.new._visit('urgent',True)
        finally:self.new._thread_priority.priority_supplier=None
        self.assertTrue(granted)
        calls=json.loads(self.new.path.read_text())['calls']
        self.assertEqual(len(calls),2)
        self.assertEqual(self.new.path,self.old.path)
        self.assertEqual(self.new.queue_path,self.old.queue_path)

    def test_exit_does_not_overtake_emergency(self):
        self.seed([('e',.2,EMERGENCY),('normal',.1,None)])
        self.new._thread_priority.priority_supplier=lambda:EXIT
        try:
            granted,_=self.new._visit('exit',None)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertFalse(granted)
        self.assertEqual([x['id'] for x in json.loads(self.new.queue_path.read_text())['waiters']],
                         ['e','normal','exit'])

    def test_waiting_normal_ages_to_prevent_starvation(self):
        self.seed([('old_normal',4.0,NORMAL),('new_exit',.2,EXIT)])
        self.new._thread_priority.priority_supplier=lambda:BACKGROUND
        try:
            granted,_=self.new._visit('bg',None)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertFalse(granted)
        q=json.loads(self.new.queue_path.read_text())['waiters']
        self.assertEqual(q[0]['id'],'old_normal')
        self.assertEqual(q[1]['id'],'new_exit')

    def test_dynamic_urgent_promotion_is_persisted(self):
        self.seed([('normal',.30,NORMAL),('wait',.25,NORMAL)])
        self.new._thread_priority.priority_supplier=lambda:EMERGENCY
        try:
            granted,_=self.new._visit('wait',True)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertTrue(granted)
        q=json.loads(self.new.queue_path.read_text())['waiters']
        self.assertEqual([x['id'] for x in q],['normal'])

    def test_expired_legacy_ticket_pruned_not_promoted(self):
        self.seed([('dead',40,None)])
        raw=json.loads(self.new.queue_path.read_text())
        raw['waiters'][0]['seen']=self.now-40
        self.new.queue_path.write_text(json.dumps(raw))
        self.new._thread_priority.priority_supplier=lambda:EXIT
        try:
            granted,_=self.new._visit('exit',None)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertTrue(granted)
        self.assertEqual(json.loads(self.new.queue_path.read_text())['waiters'],[])

    def test_bad_priority_fails_closed_without_quota_reservation(self):
        self.seed([('bad',.3,999)])
        self.new._thread_priority.priority_supplier=lambda:EXIT
        try:
            with self.assertRaises(GateUnavailable):
                self.new._visit('exit',None)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertEqual(json.loads(self.new.path.read_text())['calls'],[])

    def test_fifo_legacy_does_not_reorder_existing_priority_queue(self):
        self.seed([('emergency',.2,EMERGENCY),('normal',.5,None)])
        granted,_=self.old._visit('normal', True)
        self.assertFalse(granted)
        self.assertEqual([x['id'] for x in json.loads(self.new.queue_path.read_text())['waiters']],
                         ['emergency','normal'])

    def test_quota_limit_eight_shared_across_old_new(self):
        for _ in range(self.old.MAX_SHARED):
            granted,_=self.old._reserve_if_available(time.time())
            self.assertTrue(granted)
        self.seed([])
        self.new._thread_priority.priority_supplier=lambda:EMERGENCY
        try:
            granted,_=self.new._visit('urgent',None)
        finally:
            self.new._thread_priority.priority_supplier=None
        self.assertFalse(granted)
        self.assertEqual(len(json.loads(self.new.path.read_text())['calls']),8)

if __name__=='__main__':unittest.main()
