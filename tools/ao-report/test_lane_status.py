import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import ao_status
import maintenance


class Output(io.StringIO):
    def reconfigure(self, **kwargs): pass


class LaneStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.jobs = self.root/'jobs'
        self.jobs.mkdir()
        self.flag = self.root/'maintenance_flag.json'
        self.clock = lambda: datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
        for target, attr, value in (
            (maintenance, 'PATH', self.flag),
            (ao_status.human_outbox, 'pending_count', lambda: 2),
            (ao_status.heartbeat, 'PATH', self.root/'heartbeat.json'),
            (ao_status.reporting, 'legacy', lambda *a: False),
            (ao_status.reporting, 'closed_jobs', lambda: {}),
            (ao_status.reporting, 'latest', lambda *a: None),
            (ao_status.reporting, 'accepted_ids', lambda *a: set()),
            (ao_status.run_counter, 'lookup', lambda *a: ('key', {'runs_total':3, 'runs':[]})),
            (ao_status.coverage_lib, 'items', lambda: {'outside':{'id':'outside', 'desc':'ngoài', 'kind':'bg', 'finished_at':None}}),
            (ao_status.coverage_lib, 'stale', lambda *a: {'unreported':[], 'open':[{'id':'outside', 'desc':'ngoài'}]}),
        ):
            mock = patch.object(target, attr, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.job('JA', 'A')
        self.job('JB', 'B')
        self.job('OLD')

    def job(self, ident, lane=None, state='BLOCKED'):
        path = self.jobs/ident
        path.mkdir()
        job = {'id':ident}
        if lane: job['lane'] = lane
        (path/'job.json').write_text(json.dumps(job), encoding='utf-8')
        (path/'status.json').write_text(json.dumps({'attempt_id':ident+'#1', 'state':state,
            'started_at':'2026-10-12T01:00:00+00:00', 'finished_at':'2026-10-12T02:00:00+00:00'}), encoding='utf-8')

    def collect(self, lane=None):
        return ao_status.collect(self.jobs, self.clock, lane)

    def test_suffix_all_job_warnings_and_legacy(self):
        lines = self.collect()
        for ident, lane in [('JA','A'), ('JB','B'), ('OLD','?')]:
            matched = [line for line in lines if ident in line]
            self.assertEqual(len(matched), 5)
            self.assertTrue(all(line.endswith(' · làn '+lane) for line in matched))
        self.job('RUN', 'C', 'RUNNING')
        self.assertTrue(any(line.startswith('RUNNING quá hạn RUN') and line.endswith(' · làn C') for line in self.collect()))

    def test_filter_keeps_global_warnings(self):
        lines = self.collect('b')
        self.assertTrue(any('JB' in line for line in lines))
        self.assertFalse(any('JA' in line or 'OLD' in line for line in lines))
        for fragment in ('CHƯA GỬI ĐƯỢC', 'BỘ GÁC CHẾT?', 'NGOÀI ao_run'):
            self.assertTrue(any(fragment in line for line in lines))

    def test_cli_filter_header_exit_and_limit(self):
        real_collect = ao_status.collect
        out = Output()
        with patch.object(ao_status, 'collect', side_effect=lambda lane=None: real_collect(self.jobs, self.clock, lane)), contextlib.redirect_stdout(out):
            self.assertEqual(ao_status.main(['--lane','b']), 1)
        self.assertTrue(out.getvalue().startswith('AO STATUS: CÓ CẢNH BÁO\n'))
        self.assertIn('JB', out.getvalue())
        self.assertNotIn('JA', out.getvalue())
        out = Output()
        with patch.object(ao_status, 'collect', return_value=[str(i) for i in range(30)]), contextlib.redirect_stdout(out):
            self.assertEqual(ao_status.main([]), 1)
        self.assertEqual(len(out.getvalue().splitlines()), 25)
        self.assertIn('... và 7 cảnh báo khác', out.getvalue())
        with patch.object(ao_status, 'collect', return_value=[]), contextlib.redirect_stdout(Output()):
            self.assertEqual(ao_status.main([]), 0)

    def test_maintenance_on_off_show_and_first_when_filtered(self):
        with contextlib.redirect_stdout(Output()):
            self.assertEqual(maintenance.main(['on','--why','kiểm tra tiếng Việt','--by','Codex']), 0)
        data = maintenance.read()
        self.assertIs(data['on'], True)
        self.assertEqual(data['by'], 'Codex')
        self.assertIsNotNone(datetime.fromisoformat(data['at']).tzinfo)
        self.assertTrue(self.collect('B')[0].startswith('ĐANG BẢO TRÌ làn A'))
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        out = Output()
        with contextlib.redirect_stdout(out): maintenance.main(['show'])
        self.assertIn('kiểm tra tiếng Việt', out.getvalue())
        with contextlib.redirect_stdout(Output()): maintenance.main(['off'])
        self.assertIs(maintenance.read()['on'], False)
        self.assertFalse(any('ĐANG BẢO TRÌ' in line for line in self.collect()))

    def test_corrupt_unreadable_and_missing_flag_ignored(self):
        for content in ('{broken', '[]', '{"on":true}', '{"on":true,"at":"bad","why":"x"}',
                        '{"on":true,"at":"0001-01-01T00:00:00+14:00","why":"x"}'):
            self.flag.write_text(content, encoding='utf-8')
            self.assertFalse(any('ĐANG BẢO TRÌ' in line for line in self.collect()))
        with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
            self.assertIsNone(maintenance.warning())
        self.assertIsNone(maintenance.warning(self.root/'missing.json'))

    def test_atomic_failure_preserves_flag_and_vietnam_time(self):
        data = {'on':True, 'at':'2026-10-12T08:00:00+00:00', 'why':'thử', 'by':'Codex'}
        maintenance.write(data)
        self.assertIn('từ 12/10/2026 15:00', maintenance.warning())
        before = self.flag.read_bytes()
        with patch.object(maintenance.os, 'replace', side_effect=OSError('test replacement failure')):
            with self.assertRaises(OSError): maintenance.write({'on':False})
        self.assertEqual(self.flag.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_counter_error_suffix(self):
        with patch.object(ao_status.run_counter, 'lookup', side_effect=ValueError('broken')):
            lines = self.collect('A')
        self.assertIn('JA: không đọc được bộ đếm lần chạy · làn A', lines)


if __name__ == '__main__': unittest.main()
