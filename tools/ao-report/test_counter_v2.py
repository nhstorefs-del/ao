import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import counter_admin
import error_classifier
import learn
import recovery_policy
import reporting
import run_counter


class CounterV2(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        brief = self.root / 'brief.md'
        brief.write_text('same logical task', encoding='utf-8')
        self.job = {'id': 'ONE', 'attempt_id': 'ONE#1', 'brief': str(brief),
                    'signals': ['file:' + str(self.root / 'result.json')], 'max_retries': 10}
        self.path = self.root / 'counter.json'
        self.time = datetime(2026, 10, 12, tzinfo=timezone.utc)

    def test_one_counter_for_all_recovery_types_and_new_id(self):
        reasons = ['INITIAL', 'TRANSIENT', 'QUOTA']
        for n, reason in enumerate(reasons, 1):
            self.job['attempt_id'] = f'ONE#{n}'
            key, total, effective = run_counter.reserve(self.job, 'codex-2', reason, self.path, self.time)
            self.assertEqual((total, effective), (n, 3))
        self.job['id'] = 'TWO'
        self.job['attempt_id'] = 'TWO#1'
        with self.assertRaisesRegex(ValueError, '3/3'):
            run_counter.reserve(self.job, 'deepseek-paid', 'QC_FAIL', self.path, self.time)
        self.assertEqual(run_counter.lookup(self.job, self.path, self.time)[0], key)
        self.assertEqual(run_counter.read(self.path)['groups'][key]['runs_total'], 3)

    def test_persistence_reset_and_corrupt_fail_closed(self):
        key, _, _ = run_counter.reserve(self.job, 'codex-2', 'INITIAL', self.path, self.time)
        self.assertEqual(run_counter.lookup(self.job, self.path, self.time)[1]['runs_total'], 1)
        with self.assertRaises(ValueError): counter_admin.reset(key, '', self.path)
        counter_admin.reset(key, 'Owner duyệt reset việc này', self.path)
        self.assertEqual(run_counter.read(self.path)['groups'][key]['runs_total'], 0)
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError): run_counter.reserve(self.job, 'codex-2', 'INITIAL', self.path, self.time)

    def test_same_id_never_resets_and_new_id_after_25_hours_does(self):
        old_key, _, _ = run_counter.reserve(self.job, 'codex-2', 'INITIAL', self.path, self.time)
        later = self.time + timedelta(hours=25)
        self.assertEqual(run_counter.lookup(self.job, self.path, later)[0], old_key)
        self.job['id'] = 'NEW'; self.job['attempt_id'] = 'NEW#1'
        new_key, total, _ = run_counter.reserve(self.job, 'codex-2', 'INITIAL', self.path, later)
        self.assertNotEqual(old_key, new_key)
        self.assertEqual(total, 1)

    def test_brief_can_only_reduce_and_policy_lock(self):
        self.job['max_retries'] = 0
        _, total, limit = run_counter.reserve(self.job, 'codex-2', 'INITIAL', self.path, self.time)
        self.assertEqual((total, limit), (1, 1))
        with self.assertRaises(ValueError): run_counter.reserve(self.job, 'codex-2', 'TRANSIENT', self.path, self.time)
        with patch.object(recovery_policy, 'policy_digest', return_value='tampered'):
            with self.assertRaises(ValueError): recovery_policy.verify_lock()

    def test_pending_learning_three_uses_and_owner_decision(self):
        patterns = self.root / 'patterns.json'
        patterns.write_text(error_classifier.PATTERNS.read_text(encoding='utf-8'), encoding='utf-8')
        unknown = self.root / 'unknown.jsonl'
        sig = error_classifier.classify('w', 'peculiar failure', 1, patterns=patterns)['signature']
        reporting.append(unknown, {'signature': sig, 'worker': 'w', 'reason': 'peculiar failure'})
        with self.assertRaises(ValueError):
            learn.resolve(sig, 'retry', 'Owner không duyệt thử lại', auto=True,
                          patterns=patterns, unknown=unknown)
        entry = learn.resolve(sig, 'retry', 'Owner duyệt thử lại lỗi này', auto=True,
                              patterns=patterns, unknown=unknown)
        self.assertEqual(entry['state'], 'PENDING_CONFIRMATION')
        for _ in range(3):
            self.assertEqual(error_classifier.classify('w', 'peculiar failure', 1,
                                                       patterns=patterns)['approved_action'], 'retry')
        result = error_classifier.classify('w', 'peculiar failure', 1, patterns=patterns)
        self.assertIsNone(result['approved_action'])
        self.assertEqual(result['type'], 'NEEDS_HUMAN')
        with self.assertRaises(ValueError): learn.change(sig, 'CONFIRMED', '', patterns)
        self.assertEqual(learn.change(sig, 'REJECTED', 'Owner duyệt bác bỏ', patterns)['state'], 'REJECTED')


if __name__ == '__main__': unittest.main()
