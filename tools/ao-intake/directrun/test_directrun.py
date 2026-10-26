import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import directrun_hook as hook
from directrun_lib import detect, safe_command
from report import summary


def payload(command):
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'PowerShell',
            'tool_input': {'command': command}, 'session_id': 's1'}


class DirectRunTests(unittest.TestCase):
    def test_detect_positive(self):
        for command in ('codex exec x', '& codex exec x', 'codex', 'claude -p "x"',
                        'claude --print x', 'qwen -p x', 'deepseek chat',
                        'cd x && codex exec x', 'python haiku_worker.py',
                        'python supervisor.py', 'echo ok | codex exec x',
                        'C:\\bin\\codex.cmd exec x', 'qwen.ps1 -p x'):
            with self.subTest(command=command):
                self.assertIsNotNone(detect(command))

    def test_detect_negative(self):
        for command in ('python ao_run.py start x', 'python ao_supervisor.py x',
                        'grep codex file', 'echo "codex exec"',
                        'git commit -m "claude -p"', 'codex --version',
                        'qwen --help', 'AO_INTAKE_REQUEST=1 codex exec x',
                        'echo "a && codex exec"'):
            with self.subTest(command=command):
                self.assertIsNone(detect(command))

    def test_modes_and_log(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            p = payload('codex exec "tiếng Việt" --api-key=EXAMPLE_API_KEY')
            result = hook.process(p, base=base, environ={})
            self.assertNotIn('permissionDecision', result['hookSpecificOutput'])
            log = (base / 'data' / 'directrun.jsonl').read_text(encoding='utf-8')
            self.assertIn('tiếng Việt', log)
            self.assertNotIn('EXAMPLE_API_KEY', log)
            (base / 'MODE').write_text('rác', encoding='utf-8')
            self.assertNotIn('permissionDecision', hook.process(p, base=base, environ={})['hookSpecificOutput'])
            (base / 'MODE').write_text('block', encoding='utf-8')
            self.assertEqual(hook.process(p, base=base, environ={})['hookSpecificOutput']['permissionDecision'], 'deny')
            (base / 'ALLOW_ONCE').touch()
            self.assertNotIn('permissionDecision', hook.process(p, base=base, environ={})['hookSpecificOutput'])
            self.assertEqual(len(summary(base / 'data' / 'directrun.jsonl').splitlines()), 7)

    def test_env_and_off(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'MODE').write_text('block', encoding='utf-8')
            self.assertEqual(hook.process(payload('codex exec x'), base=base,
                                          environ={'AO_RUN_ID': 'x'}), {})
            self.assertEqual(hook.process(payload('codex exec x'), base=base,
                                          environ={'AO_DIRECTRUN_HOOK': '0'}), {})
            (base / 'MODE').write_text('off', encoding='utf-8')
            self.assertEqual(hook.process(payload('codex exec x'), base=base, environ={}), {})

    def test_bad_json_fail_open(self):
        class Input:
            buffer = io.BytesIO(b'{bad')
        out = io.StringIO()
        with patch.object(sys, 'stdin', Input()), contextlib.redirect_stdout(out):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(out.getvalue(), '{}')

    def test_mask(self):
        self.assertNotIn('abc123', safe_command('codex exec --token=abc123'))
        self.assertNotIn('abc123', safe_command('codex exec --token abc123'))
        self.assertNotIn('abc123', safe_command('codex exec -H "Bearer abc123"'))

    def test_log_failure_opens(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'MODE').write_text('block', encoding='utf-8')
            (base / 'data').write_text('not a directory', encoding='utf-8')
            # The entry point catches failures and returns an empty decision.
            original = hook.process
            with patch.object(hook, 'process', side_effect=lambda p: original(p, base=base, environ={})), patch.object(sys, 'stdin',
                    type('Input', (), {'buffer': io.BytesIO(json.dumps(payload('codex exec x')).encode())})()), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(hook.main(), 0)
            self.assertEqual(output.getvalue(), '{}')


if __name__ == '__main__':
    unittest.main()
