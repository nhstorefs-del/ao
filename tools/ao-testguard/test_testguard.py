import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import testguard as guard


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.test = self.root / 'test_old.py'
        self.test.write_text('def test_ok():\n    assert 1 == 2\n', encoding='utf-8')
        self.snap = self.root / 'snap.json'
        guard.snapshot(self.root, ['test_*.py'], self.snap)

    def write(self, text, name='test_old.py'):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def check(self, allow=False):
        return guard.check(self.root, self.snap, allow)

    def rules(self, allow=False):
        return [v['rule'] for v in self.check(allow)['violations']]

    def test_unchanged(self):
        self.assertEqual(self.check()['status'], 'clean')

    def test_modified(self):
        self.write('def test_ok():\n    assert 2 == 3\n')
        self.assertIn('tệp bị sửa', self.rules())

    def test_deleted(self):
        self.test.unlink()
        self.assertIn('tệp bị xóa', self.rules())

    def test_skip_line(self):
        self.write('import unittest\n' + '@unittest.skip("reason")\ndef test_ok():\n    assert 1 == 2\n')
        hit = [v for v in self.check()['violations'] if v['rule'] == 'python-skip-xfail'][0]
        self.assertEqual((hit['file'], hit['line']), ('test_old.py', 2))

    def test_assert_true(self):
        self.write('def test_ok():\n    assert True\n')
        self.assertIn('assert-true', self.rules())

    def test_js_only_xit(self):
        self.write('test.only("a", () => {});\nxit("b", () => {});', 'tests.js')
        guard.snapshot(self.root, ['test_*.py', '*.js'], self.snap)
        self.write('test.only("c", () => {});\nxit("d", () => {});\nxdescribe("z",()=>{});\ntest.only("e",()=>{});', 'tests.js')
        rules = self.rules()
        self.assertIn('js-skip-only', rules)
        self.assertIn('js-disabled-test', rules)

    def test_baseline_signs_not_new(self):
        self.write('import unittest\n' + '@unittest.skip("old")\ndef test_ok():\n    assert True\n')
        guard.snapshot(self.root, ['test_*.py'], self.snap)
        self.assertEqual(self.rules(), [])

    def test_new_file_rejected_or_allowed(self):
        self.write('def test_new():\n    assert 3 == 4\n', 'test_new.py')
        self.assertIn('tệp mới chưa được phép', self.rules())
        self.assertEqual(self.rules(True), [])

    def test_new_weak_file_even_allowed(self):
        self.write('def test_new():\n    assert True\n', 'test_new.py')
        self.assertIn('assert-true', self.rules(True))

    def test_empty_glob(self):
        with self.assertRaisesRegex(guard.GuardError, 'không khớp'):
            guard.snapshot(self.root, ['missing_*.py'], self.snap)

    def test_invalid_encoding(self):
        self.test.write_bytes(b'\xff\xfe')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = guard.main(['check', '--root', str(self.root), '--snapshot', str(self.snap), '--json'])
        self.assertEqual(code, 2)
        self.assertIn('UTF-8', json.loads(output.getvalue())['error'])

    def test_python_required_patterns(self):
        snippets = {
            '@pytest.mark.skipif(True)\ndef test_a():\n    assert 1 == 2': 'python-skip-xfail',
            '@pytest.mark.xfail\ndef test_a():\n    assert 1 == 2': 'python-skip-xfail',
            'pytest.skip("x")': 'python-skip-call',
            'self.skipTest("x")': 'python-skip-call',
            'self.assertTrue(True)': 'assert-true',
            'def test_a():\n    pass': 'test-only-pass',
            'try:\n    assert 1 == 2\nexcept:\n    pass': 'except-pass-around-assert',
            'assert 7 == 7': 'constant-equals-itself',
            'self.assertEqual("same", "same")': 'constant-equals-itself',
        }
        for source, rule in snippets.items():
            with self.subTest(source=source):
                self.assertIn(rule, [s['rule'] for s in guard.signs(source, '.py')])

    def test_js_required_patterns(self):
        for source in ['test.skip("x", fn)', 'test.only("x", fn)', 'xit("x", fn)', 'xdescribe("x", fn)', 'expect(true).toBe(true)', 'assert(true)', 'assert(42 === 42)']:
            with self.subTest(source=source):
                self.assertGreater(len(guard.signs(source, '.ts')), 0)

    def test_comments_strings_and_real_assertions(self):
        self.assertEqual(guard.signs('# assert True\ns = "pytest.skip(1)"\nassert actual == 1', '.py'), [])
        self.assertEqual(guard.signs('// xit(\nconst s = "test.only(";\nexpect(actual).toBe(true)', '.js'), [])
        self.assertEqual(guard.signs('assert x == x', '.py'), [])

    def test_baseline_moved_and_duplicate(self):
        self.write('assert True\n')
        guard.snapshot(self.root, ['test_*.py'], self.snap)
        self.write('\n\nassert True\nassert True\n')
        signs = [v for v in self.check()['violations'] if v['rule'] == 'assert-true']
        self.assertEqual(len(signs), 1)
        self.assertEqual(signs[0]['line'], 4)

    def test_nested_glob(self):
        (self.root / 'nested').mkdir()
        self.write('assert 1 == 2', 'nested/test_nested.py')
        guard.snapshot(self.root, ['**/test_*.py'], self.snap)
        self.assertEqual(guard.check(self.root, self.snap)['count'], 2)

    def test_cli_codes_and_output_limit(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['check', '--root', str(self.root), '--snapshot', str(self.snap)]), 0)
        self.write('assert True\n' * 40)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(guard.main(['check', '--root', str(self.root), '--snapshot', str(self.snap)]), 3)
        self.assertLess(len(output.getvalue().splitlines()), 30)

    def test_corrupt_snapshot(self):
        self.snap.write_text('{bad', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['check', '--root', str(self.root), '--snapshot', str(self.snap)]), 2)

    def test_invalid_syntax_fails_closed(self):
        self.write('def test_bad(:')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['check', '--root', str(self.root), '--snapshot', str(self.snap)]), 2)

    def test_cli_empty_glob_is_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = guard.main(['snapshot', '--root', str(self.root), '--glob', 'missing*.py', '--out', str(self.snap)])
        self.assertEqual(code, 2)
        self.assertIn('không khớp', output.getvalue())


if __name__ == '__main__':
    unittest.main()
