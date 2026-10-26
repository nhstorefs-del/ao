import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from integrity_check import baseline, check
from integrity_ledger import read


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'baseline.jsonl'
        self.drift = self.root / 'drift.jsonl'
        self.settings = self.root / '.claude/settings.json'
        self.hook = self.root / '.claude/hooks/start.py'
        self.settings.parent.mkdir(parents=True)
        self.hook.parent.mkdir(parents=True)
        self.settings.write_bytes(b'{}')
        self.hook.write_bytes(b'print(1)')

    def run_check(self, brief=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = check(self.root, self.ledger, drift=self.drift, brief=brief)
        return code, output.getvalue()

    def make_baseline(self):
        return baseline(self.root, self.ledger, 'test', bootstrap=True, note='Owner test approval')

    def test_missing_and_clean(self):
        self.assertEqual(self.run_check()[0], 8)
        self.assertFalse(self.drift.exists())
        self.make_baseline()
        self.assertEqual(self.run_check()[0], 0)

    def test_change_add_delete_and_deduplicate(self):
        self.make_baseline()
        self.settings.write_bytes(b'{}x')
        code, output = self.run_check()
        self.assertEqual(code, 9)
        self.assertIn('.claude/settings.json', output)
        self.assertIn('sửa:', output)
        self.run_check()
        self.assertEqual(len(self.drift.read_text(encoding='utf-8').splitlines()), 1)
        self.settings.write_bytes(b'{}')
        extra = self.hook.parent / 'new.py'
        extra.write_bytes(b'hi')
        self.assertIn('thêm: .claude/hooks/new.py', self.run_check()[1])
        extra.unlink()
        self.hook.unlink()
        self.assertIn('xóa: .claude/hooks/start.py', self.run_check()[1])

    def test_corrupted_chain(self):
        self.make_baseline()
        raw = self.ledger.read_text(encoding='utf-8')
        self.ledger.write_text(raw.replace('test', 'tampered'), encoding='utf-8')
        self.assertEqual(self.run_check()[0], 8)

    def test_brief_and_ignored(self):
        self.make_baseline()
        cache = self.hook.parent / '__pycache__'
        cache.mkdir()
        (cache / 'new.py').write_bytes(b'x')
        (self.hook.parent / 'start.py.bak_1013').write_bytes(b'x')
        self.assertEqual(self.run_check()[0], 0)
        self.hook.write_bytes(b'x')
        code, output = self.run_check(True)
        self.assertEqual(code, 9)
        self.assertLessEqual(len(output.splitlines()), 3)

    def test_approval_and_uses(self):
        self.make_baseline()
        with self.assertRaises(ValueError):
            baseline(self.root, self.ledger, 'update')
        from approval_ledger import Ledger
        approvals = Ledger(self.root / 'approvals.jsonl')
        item = approvals.record('test-session', 'APPROVAL BY OWNER cập nhật đường chuẩn kiểm hash REQ-ab12',
                                ['REQ-ab12'], uses=1)
        baseline(self.root, self.ledger, 'update', approval_id=item['id'],
                 request_id='REQ-ab12', approval_ledger=approvals.path)
        self.assertEqual(read(self.ledger)[-1]['approved_by'], item['id'])
        self.assertEqual(approvals.get(item['id'])['uses_left'], 0)
        with self.assertRaises(ValueError):
            baseline(self.root, self.ledger, 'again', approval_id=item['id'],
                     request_id='REQ-ab12', approval_ledger=approvals.path)
        self.assertEqual(len(read(self.ledger)), 2)

    def test_bootstrap_once(self):
        self.make_baseline()
        with self.assertRaises(ValueError):
            self.make_baseline()

    def test_patches_apply_to_temporary_copies(self):
        project = Path(__file__).resolve().parents[3]
        for name, target in [('ao_status_integrity.patch', 'ao_status.py'),
                             ('watchdog_integrity.patch', 'watchdog.py')]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                copy = Path(temporary) / 'tools/ao-report'
                copy.mkdir(parents=True)
                shutil.copy2(project / 'tools/ao-report' / target, copy / target)
                patch = Path(__file__).resolve().parent / 'patches' / name
                result = subprocess.run(['git', 'apply', '--check', str(patch)], cwd=temporary,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
