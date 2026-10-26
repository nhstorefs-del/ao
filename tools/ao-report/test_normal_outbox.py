import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import normal_outbox
import reporting


class NormalOutboxTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.outbox=self.root/'normal.jsonl'
        self.sent=self.root/'sent.jsonl'
        self.time=datetime(2026,10,12,9,0,tzinfo=timezone(timedelta(hours=7)))

    def job(self, ident, minute):
        path=self.root/ident; path.mkdir()
        artifact=self.root/(ident+'.RESULT.json')
        artifact.write_text(json.dumps({'status':'pass','summary':'RESULT'}),encoding='utf-8')
        (path/'job.json').write_text(json.dumps({'id':ident,'title':'Việc '+ident,
            'signals':['file:'+str(artifact)]}),encoding='utf-8')
        (path/'status.json').write_text(json.dumps({'id':ident,'state':'DONE_NEEDS_REVIEW',
            'attempt_id':ident+'#1','owner_notified':False,
            'finished_at':(self.time+timedelta(minutes=minute)).isoformat()}),encoding='utf-8')
        return path

    def test_five_jobs_one_text_five_ledger_rows_no_duplicates(self):
        paths=[self.job('J'+str(i),i) for i in range(5)]
        for i,path in enumerate(paths):
            self.assertEqual(normal_outbox.enqueue(path,lambda:self.time+timedelta(minutes=i),
                self.outbox,launch=False),'queued')
        self.assertEqual(normal_outbox.flush(sender=lambda _:0,
            clock=lambda:self.time+timedelta(minutes=9),outbox=self.outbox,sent=self.sent),0)
        texts=[]
        self.assertEqual(normal_outbox.flush(sender=lambda body:texts.append(body) or 0,
            clock=lambda:self.time+timedelta(minutes=11),outbox=self.outbox,sent=self.sent),5)
        self.assertEqual(len(texts),1)
        self.assertEqual(len(texts[0].splitlines()),7)
        self.assertEqual(len(reporting.rows(self.sent)),5)
        self.assertEqual(normal_outbox.flush(sender=lambda body:texts.append(body) or 0,
            clock=lambda:self.time+timedelta(minutes=30),outbox=self.outbox,sent=self.sent),0)
        self.assertEqual(len(texts),1)
        self.assertTrue(all(reporting.read(p/'status.json')['owner_notified'] for p in paths))

    def test_failed_send_stays_pending_and_does_not_fail_job(self):
        path=self.job('FAIL',0)
        normal_outbox.enqueue(path,lambda:self.time,self.outbox,launch=False)
        self.assertEqual(normal_outbox.flush(sender=lambda _:99,
            clock=lambda:self.time+timedelta(minutes=11),outbox=self.outbox,sent=self.sent),0)
        self.assertFalse(reporting.read(path/'status.json')['owner_notified'])
        self.assertEqual(reporting.read(path/'status.json')['state'],'DONE_NEEDS_REVIEW')
        self.assertEqual(normal_outbox.flush(sender=lambda _:0,
            clock=lambda:self.time+timedelta(minutes=12),outbox=self.outbox,sent=self.sent),0)
        self.assertEqual(normal_outbox.flush(sender=lambda _:0,
            clock=lambda:self.time+timedelta(minutes=16),outbox=self.outbox,sent=self.sent),1)


if __name__=='__main__': unittest.main()
