"""Offline cycle tests: never invoke Claude or real AO jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import run_dispatch_cycle as cycle
from store import Store


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=cycle.BASE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / 'test.db'
        self.scan = Mock(return_value=[])
        self.dispatcher = Mock()
        self.dispatcher.once.return_value = []
        self.factory = Mock(return_value=self.dispatcher)

    def run_cycle(self):
        return cycle.run_cycle(self.db, self.base, self.scan, self.factory)

    def queued(self):
        Store(self.db).submit('Offline test only', 'test-key')

    def log(self):
        return (self.base / 'logs' / 'cycle.log').read_text(encoding='utf-8')

    def test_empty_no_dispatcher_or_recovery(self):
        with patch.object(cycle.Dispatcher, 'execute', side_effect=AssertionError('Claude forbidden')):
            self.assertEqual(self.run_cycle(), 0)
        self.factory.assert_not_called()
        self.scan.assert_not_called()
        self.assertIn('idle queued=0 active=0', self.log())

    def test_pause_no_writes(self):
        (self.base / 'PAUSE').touch()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([p.name for p in self.base.iterdir()], ['PAUSE'])
        self.factory.assert_not_called()
        self.scan.assert_not_called()

    def test_live_lock_second_cycle_exits(self):
        lock = cycle.CycleLock(self.base / 'cycle.lock')
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        # Windows byte locks also block reads of byte zero from other handles.
        with lock.path.open('rb') as f:
            f.seek(1)
            original = f.read()
        self.assertEqual(self.run_cycle(), 0)
        with lock.path.open('rb') as f:
            f.seek(1)
            self.assertEqual(f.read(), original)
        self.assertFalse(self.db.exists())
        self.scan.assert_not_called()

    def test_dead_process_lock_reclaimed(self):
        # Real child exits without release: kernel must release its lock.
        code = "import os; from pathlib import Path; from run_dispatch_cycle import CycleLock; l=CycleLock(Path(os.environ['TEST_CYCLE_LOCK'])); assert l.acquire(); os._exit(0)"
        env = dict(os.environ, TEST_CYCLE_LOCK=str(self.base / 'cycle.lock'),
                   PYTHONDONTWRITEBYTECODE='1')
        child = subprocess.run([sys.executable, '-B', '-c', code], cwd=cycle.BASE,
                               env=env, capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        old = json.loads((self.base / 'cycle.lock').read_bytes()[1:])
        self.assertNotEqual(old['pid'], os.getpid())
        self.assertEqual(self.run_cycle(), 0)
        new = json.loads((self.base / 'cycle.lock').read_bytes()[1:])
        self.assertEqual(new['pid'], os.getpid())
        self.assertIn('idle', self.log())

    def test_recover_error_dispatch_continues_without_sensitive_log(self):
        self.queued()
        self.scan.side_effect = RuntimeError('SECRET REQUEST TEXT')
        self.assertEqual(self.run_cycle(), 0)
        self.dispatcher.once.assert_called_once_with()
        self.assertIn('recover error=RuntimeError', self.log())
        self.assertNotIn('SECRET', self.log())

    def test_dispatch_error_logged(self):
        self.queued()
        self.dispatcher.once.side_effect = ValueError('SECRET REQUEST TEXT')
        self.assertEqual(self.run_cycle(), 0)
        self.scan.assert_called_once_with(store=unittest.mock.ANY, apply=True,
                                          dispatcher=self.dispatcher)
        self.assertIn('dispatch error=ValueError', self.log())
        self.assertNotIn('SECRET', self.log())

    def test_running_not_treated_as_empty_and_order(self):
        store = Store(self.db)
        r = store.submit('Offline', 'active')
        store.cas(r['request_id'], 'RECORDED', 'CLAIMED')
        store.cas(r['request_id'], 'CLAIMED', 'RUNNING')
        calls = []
        self.scan.side_effect = lambda **kwargs: calls.append('recover') or []
        self.dispatcher.once.side_effect = lambda: calls.append('dispatch') or []
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(calls, ['recover', 'dispatch'])

    def test_log_rotation(self):
        path = self.base / 'logs' / 'cycle.log'
        path.parent.mkdir()
        path.write_text('x' * (1024 * 1024 + 1), encoding='utf-8')
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue(path.with_name('cycle.log.1').exists())
        self.assertLess(path.stat().st_size, 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
