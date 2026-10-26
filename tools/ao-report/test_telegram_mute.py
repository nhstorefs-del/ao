import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import check_the_checker as checker
import reporting
import telegram_mute as mute


class TelegramMuteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict(os.environ, {'AO_REPORT_SELFTEST': '0',
                                          'AO_REPORT_TEST_OUTBOX': '', 'AO_AUTOREPORT': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def send(self, text='Tin thử tiếng Việt'):
        with patch.object(reporting, 'HERE', self.root), patch.object(reporting.subprocess, 'run') as run:
            run.return_value.returncode = 7
            code = reporting.telegram(text)
        return code, run

    def test_on_records_without_subprocess(self):
        mute.set_muted(True, self.root)
        code, run = self.send()
        self.assertEqual(code, 0)
        run.assert_not_called()
        row = json.loads((self.root/'muted_outbox.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(row['text'], 'Tin thử tiếng Việt')
        self.assertEqual(row['sha256'], hashlib.sha256(row['text'].encode()).hexdigest())
        datetime.fromisoformat(row['at'])

    def test_off_and_absent_keep_original_command(self):
        for create in (False, True):
            if create:
                mute.set_muted(False, self.root)
            code, run = self.send('hello')
            self.assertEqual(code, 7)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], [reporting.sys.executable,
                str(self.root.parent/'telegram-send'/'telegram_send.py'), '--text', 'hello'])
            self.assertEqual(run.call_args.kwargs['timeout'], 75)
        self.assertFalse((self.root/'muted_outbox.jsonl').exists())

    def test_corrupt_and_invalid_schema_fail_closed(self):
        for data in (b'{broken', b'\xff', b'[]', b'{"on":"false"}', b'{}'):
            (self.root/'telegram_muted.json').write_bytes(data)
            code, run = self.send()
            self.assertEqual(code, 0)
            run.assert_not_called()

    def test_selftest_has_priority_over_mute(self):
        mute.set_muted(True, self.root)
        outbox = self.root/'selftest.jsonl'
        with patch.dict(os.environ, {'AO_REPORT_SELFTEST': '1', 'AO_REPORT_TEST_OUTBOX': str(outbox)}):
            code, run = self.send('selftest')
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertEqual(json.loads(outbox.read_text(encoding='utf-8'))['text'], 'selftest')
        self.assertFalse((self.root/'muted_outbox.jsonl').exists())

    def test_checker_on_off_and_corrupt(self):
        for index, data in enumerate(('{"on":true}', '{"on":false}', 'broken')):
            (self.root/'telegram_muted.json').write_text(data, encoding='utf-8')
            with patch.object(checker, 'HERE', self.root), patch.object(checker.subprocess, 'run') as run:
                run.return_value.returncode = 0
                result = checker.run(now=datetime(2026, 10, 12, 8, tzinfo=timezone.utc),
                    heart=self.root/'missing.json', state=self.root/f'state{index}.json')
            self.assertTrue(result['sent'])
            self.assertEqual(run.call_count, 1 if index == 1 else 0)
        rows = (self.root/'muted_outbox.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(json.loads(row)['text'].startswith('[GIÁM SÁT]') for row in rows))

    def test_atomic_on_off_utf8_and_no_temporary_left(self):
        with patch.object(mute.os, 'replace', wraps=mute.os.replace) as replace:
            mute.set_muted(True, self.root)
            self.assertTrue(mute.is_muted(self.root))
            mute.set_muted(False, self.root)
            self.assertFalse(mute.is_muted(self.root))
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_checker_selftest_precedes_flag(self):
        mute.set_muted(True, self.root)
        outbox = self.root/'selftest.jsonl'
        with patch.dict(os.environ, {'AO_REPORT_SELFTEST': '1', 'AO_REPORT_TEST_OUTBOX': str(outbox)}), \
                patch.object(checker, 'HERE', self.root), patch.object(checker.subprocess, 'run') as run:
            result = checker.run(now=datetime(2026, 10, 12, 8, tzinfo=timezone.utc),
                heart=self.root/'missing.json', state=self.root/'state.json')
        run.assert_not_called()
        self.assertTrue(result['sent'])
        self.assertTrue(json.loads(outbox.read_text(encoding='utf-8'))['text'].startswith('[GIÁM SÁT]'))
        self.assertFalse((self.root/'muted_outbox.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
