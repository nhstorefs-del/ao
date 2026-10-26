import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import reporting
import watchdog
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ao-memory'))
import ao_supervisor


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.sent = self.root / 'sent.jsonl'
        self.time = datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc)
        self.path = self.make_job('TEST1')

    def make_job(self, ident, started=None, title='Việc thử'):
        path = self.jobs / ident
        path.mkdir()
        started = started or self.time - timedelta(minutes=2)
        artifact = self.root / (ident + '.json')
        artifact.write_text(json.dumps({'status': 'done', 'summary': 'Đã tạo kết quả'}), encoding='utf-8')
        (path / 'job.json').write_text(json.dumps({'id': ident, 'title': title,
            'attempt_id': ident+'#1', 'signals': ['file:'+str(artifact)],
            'journal': str(self.root / 'journal.jsonl')}), encoding='utf-8')
        (path / 'status.json').write_text(json.dumps({'id': ident, 'state': 'DONE_NEEDS_REVIEW',
            'attempt_id': ident+'#1', 'finished_at': self.time.isoformat(),
            'started_at': started.isoformat()}), encoding='utf-8')
        return path

    def test_once_per_attempt_and_supervisor_without_status_call(self):
        sent = []
        sender = lambda body: sent.append(body) or 0
        for _ in range(2):
            reporting.notify(self.path, sender=sender, sent=self.sent, clock=lambda: self.time)
        self.assertEqual(len(sent), 1)
        self.assertTrue(reporting.read(self.path / 'status.json')['owner_notified'])
        self.assertIn('Chưa kiểm độc lập', sent[0])

    def test_supervisor_terminal_transition_sends_without_coordinator(self):
        calls = []
        original = reporting.notify
        with patch.object(reporting, 'notify', lambda path, state: original(
                path, state, sender=lambda body: calls.append(body) or 0,
                sent=self.sent, clock=lambda: self.time)):
            ao_supervisor.status(self.path, state='DONE_NEEDS_REVIEW')
        self.assertEqual(len(calls), 1)
        self.assertTrue(reporting.read(self.path / 'status.json')['owner_notified'])

    def test_failed_send_pending_retry_and_job_unchanged(self):
        codes = iter([5, 5, 0])
        first = reporting.notify(self.path, sender=lambda _: next(codes), sent=self.sent, clock=lambda: self.time)
        self.assertEqual(first, 'pending')
        self.assertEqual(reporting.read(self.path / 'status.json')['state'], 'DONE_NEEDS_REVIEW')
        self.assertEqual(reporting.notify(self.path, sender=lambda _: 0, sent=self.sent, clock=lambda: self.time), 'pending')
        self.assertEqual(reporting.notify(self.path, sender=lambda _: next(codes), sent=self.sent,
                          clock=lambda: self.time+timedelta(minutes=5)), 'pending')
        self.assertEqual(reporting.notify(self.path, sender=lambda _: next(codes), sent=self.sent,
                          clock=lambda: self.time+timedelta(minutes=10)), 'sent')
        self.assertEqual(len(reporting.rows(self.sent)), 3)

    def test_sender_exception_is_pending(self):
        def broken(_):
            raise OSError('network down')
        self.assertEqual(reporting.notify(self.path, sender=broken, sent=self.sent,
                         clock=lambda: self.time), 'pending')
        self.assertEqual(reporting.rows(self.sent)[-1]['result_code'], 99)
        self.assertEqual(reporting.read(self.path / 'status.json')['state'], 'DONE_NEEDS_REVIEW')

    def test_legacy_ignored_and_logged_by_watchdog(self):
        self.make_job('OLD', started=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        calls = []
        legacy = self.root / 'legacy.json'
        watchdog.run(self.jobs, sender=lambda x: calls.append(x) or 0, sent=self.sent,
                     clock=lambda: self.time, legacy_file=legacy, reminder_file=self.root/'reminders.jsonl')
        self.assertEqual(len(calls), 1)
        self.assertIn('OLD', reporting.read(legacy))

    def test_watchdog_backfill_and_reminder_throttle(self):
        old = self.time - timedelta(minutes=70)
        data = reporting.read(self.path / 'status.json')
        data['finished_at'] = old.isoformat()
        (self.path / 'status.json').write_text(json.dumps(data), encoding='utf-8')
        calls = []
        args = dict(jobs=self.jobs, sender=lambda x: calls.append(x) or 0,
                    sent=self.sent, clock=lambda: self.time,
                    legacy_file=self.root/'legacy.json', reminder_file=self.root/'reminders.jsonl')
        watchdog.run(**args)
        watchdog.run(**args)
        self.assertEqual(len(calls), 2)  # Individual + one summary.
        self.assertIn('Còn 1 việc', calls[1])

    def test_no_secret_or_key_path(self):
        job = reporting.read(self.path / 'job.json')
        job['title'] = 'bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        (self.path / 'job.json').write_text(json.dumps(job), encoding='utf-8')
        data = self.root / 'TEST1.json'
        data.write_text(json.dumps({'summary': 'D:\\.worker-keys\\telegram.env'}), encoding='utf-8')
        calls = []
        reporting.notify(self.path, sender=lambda x: calls.append(x) or 0, sent=self.sent, clock=lambda: self.time)
        self.assertNotIn('ABCDEFGHIJKLMNOPQRSTUVWXYZ', calls[0])
        self.assertNotIn('.worker-keys', calls[0])

    def test_disabled(self):
        with patch.dict('os.environ', {'AO_AUTOREPORT': '0'}):
            self.assertEqual(reporting.notify(self.path, sender=lambda _: self.fail('sent'), sent=self.sent), 'disabled')


if __name__ == '__main__':
    unittest.main()
