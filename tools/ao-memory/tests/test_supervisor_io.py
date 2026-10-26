import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ao_supervisor


class SupervisorDiagnosticsTests(unittest.TestCase):
    def test_error_fields_and_exact_key_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            try:
                raise PermissionError('locked SECRET_CUSTOM_123 sk-example123')
            except PermissionError as exc:
                ao_supervisor.report_error(path, exc, ['SECRET_CUSTOM_123'])
            status = json.loads((path/'status.json').read_text(encoding='utf-8'))
            event = json.loads((path/'events.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(status['state'], 'SUPERVISOR_ERROR')
            self.assertEqual(status['error'], event['error'])
            self.assertEqual(status['error']['exception_type'], 'PermissionError')
            self.assertIn('test_error_fields_and_exact_key_redaction', status['error']['traceback_last'])
            self.assertIn('PermissionError', status['error']['traceback'])
            self.assertNotIn('SECRET_CUSTOM_123', json.dumps(status)+json.dumps(event))
            self.assertNotIn('sk-example123', json.dumps(status)+json.dumps(event))

class AtomicJsonTests(unittest.TestCase):
    def test_windows_sharing_violation_retries_same_temp(self):
        import common
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'status.json'
            common.atomic_json(path, {'old': True})
            replace = common.os.replace
            calls = []
            def flaky(source, destination):
                calls.append(source)
                if len(calls) <= 3:
                    self.assertEqual(common.read_json(path, {}), {'old': True})
                    exc = PermissionError('sharing violation'); exc.winerror = 32
                    raise exc
                return replace(source, destination)
            with patch.object(common, '_WINDOWS', True), patch.object(common.os, 'replace', side_effect=flaky), patch.object(common.time, 'sleep'):
                common.atomic_json(path, {'new': True})
            self.assertEqual(len(set(calls)), 1)
            self.assertEqual(common.read_json(path, {}), {'new': True})
            self.assertEqual(list(Path(folder).glob('.tmp-*')), [])

    def test_persistent_lock_raises_and_keeps_old_json(self):
        import common
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'status.json'; common.atomic_json(path, {'old': True})
            exc = PermissionError('persistent'); exc.winerror = 5
            with patch.object(common, '_WINDOWS', True), patch.object(common.os, 'replace', side_effect=exc) as replace, patch.object(common.time, 'sleep'):
                with self.assertRaises(PermissionError) as caught: common.atomic_json(path, {'new': True})
            self.assertIs(caught.exception, exc)
            self.assertEqual(replace.call_count, 11)
            self.assertEqual(common.read_json(path, {}), {'old': True})
            self.assertEqual(list(Path(folder).glob('.tmp-*')), [])

    def test_unrelated_error_is_not_retried(self):
        import common
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(common.os, 'replace', side_effect=FileExistsError('unexpected')) as replace, patch.object(common.time, 'sleep') as sleep:
                with self.assertRaises(FileExistsError): common.atomic_json(Path(folder)/'status.json', {})
            self.assertEqual(replace.call_count, 1); sleep.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows handle sharing')
    def test_real_windows_read_handle_blocks_replace_until_released(self):
        import common, ctypes, threading, time
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'; common.atomic_json(path, {'old': True})
            handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0,None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            def release():
                time.sleep(.08); kernel.CloseHandle(handle)
            thread=threading.Thread(target=release); thread.start()
            try: common.atomic_json(path, {'new': True})
            finally: thread.join()
            self.assertEqual(common.read_json(path, {}), {'new': True})

class ReadJsonTests(unittest.TestCase):
    def test_transient_windows_read_lock(self):
        import common
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'; path.write_text('{"state":"RUNNING"}', encoding='utf-8')
            original=Path.read_text; calls=[]
            def read(target, *args, **kwargs):
                calls.append(target)
                if len(calls)<3: raise PermissionError(13, 'locked')
                return original(target, *args, **kwargs)
            with patch.object(common, '_WINDOWS', True), patch.object(Path, 'read_text', read), patch.object(common.time, 'sleep'):
                self.assertEqual(common.read_json(path, {}), {'state':'RUNNING'})
            self.assertEqual(len(calls), 3)

    def test_persistent_read_lock_propagates(self):
        import common
        with patch.object(common, '_WINDOWS', True), patch.object(Path, 'read_text', side_effect=PermissionError(13, 'locked')) as read, patch.object(common.time, 'sleep'):
            with self.assertRaises(PermissionError): common.read_json('unused', {})
            self.assertEqual(read.call_count, 11)

    def test_corrupt_json_is_not_hidden(self):
        import common
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'; path.write_text('{', encoding='utf-8')
            with patch.object(common.time, 'sleep') as sleep:
                with self.assertRaises(json.JSONDecodeError): common.read_json(path, {})
                sleep.assert_not_called()
            self.assertEqual(common.read_json(Path(folder)/'missing', {'default':True}), {'default':True})
