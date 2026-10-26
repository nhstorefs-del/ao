import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from common import hidden_popen_kwargs, pid_alive, process_start_time, read_json
from window_probe import WindowSampler


class HiddenWindowsTests(unittest.TestCase):
    def test_non_windows(self):
        with patch('common.os.name', 'posix'):
            self.assertEqual(hidden_popen_kwargs(), {})
            self.assertEqual(hidden_popen_kwargs(detached=True, new_process_group=True), {})

    @unittest.skipUnless(os.name == 'nt', 'Windows creation flags')
    def test_windows_flags(self):
        kwargs = hidden_popen_kwargs(new_process_group=True)
        self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(kwargs['creationflags'] & subprocess.DETACHED_PROCESS)
        self.assertNotIn('startupinfo', kwargs)  # GUI apps such as Explorer keep their UI.
        detached = hidden_popen_kwargs(detached=True, new_process_group=True)
        self.assertTrue(detached['creationflags'] & subprocess.DETACHED_PROCESS)
        self.assertFalse(detached['creationflags'] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(detached['startupinfo'].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(detached['startupinfo'].wShowWindow, subprocess.SW_HIDE)
        self.assertFalse(hidden_popen_kwargs(detached=True, creationflags=subprocess.CREATE_NO_WINDOW)['creationflags'] & subprocess.CREATE_NO_WINDOW)
        for flag in (subprocess.DETACHED_PROCESS, subprocess.CREATE_NEW_CONSOLE):
            with self.assertRaises(ValueError):
                hidden_popen_kwargs(creationflags=flag)

    @unittest.skipUnless(os.name == 'nt', 'Windows visible-window measurement')
    def test_detached_parent_and_cmd_worker_hidden_with_logs_and_exit(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root) as folder:
            base = Path(folder)
            worker = base/'worker.cmd'
            worker.write_text('@echo off\n"'+sys.executable+'" -c "import sys,time; print(\'stdout marker\',flush=True); print(\'stderr marker\',file=sys.stderr,flush=True); time.sleep(1); sys.exit(7)"\n', encoding='utf-8')
            parent = base/'parent.py'
            parent.write_text('import sys,subprocess\nsys.path.insert(0,sys.argv[1])\nfrom common import hidden_popen_kwargs\np=subprocess.run([sys.argv[2]],capture_output=True,**hidden_popen_kwargs(new_process_group=True))\nfrom pathlib import Path\nPath(sys.argv[3]).write_bytes(p.stdout+p.stderr)\nsys.exit(p.returncode)\n', encoding='utf-8')
            with WindowSampler() as sampler:
                with subprocess.Popen([sys.executable,str(parent),str(root),str(worker),str(base/'log')], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_popen_kwargs(detached=True, new_process_group=True)) as process:
                    self.assertEqual(process.wait(timeout=10), 7)
            self.assertGreater(sampler.samples, 5)
            self.assertEqual(sampler.seen, {})
            log = (base/'log').read_text()
            self.assertIn('stdout marker', log)
            self.assertIn('stderr marker', log)

    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_kill_tree_hidden_worker_and_descendant(self):
        from ao_supervisor import kill_tree
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root) as folder:
            pidfile = Path(folder)/'child.json'
            code = ('import sys,subprocess,time,json; from pathlib import Path; '
                    'from common import hidden_popen_kwargs,process_start_time; '
                    'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],**hidden_popen_kwargs()); '
                    'Path(sys.argv[1]).write_text(json.dumps([p.pid,process_start_time(p.pid)])); time.sleep(30)')
            with WindowSampler() as sampler:
                with subprocess.Popen([sys.executable,'-c',code,str(pidfile)], cwd=root, **hidden_popen_kwargs(new_process_group=True)) as process:
                    try:
                        deadline=time.monotonic()+5
                        while not pidfile.exists() and time.monotonic()<deadline:
                            time.sleep(.1)
                        child, created = read_json(pidfile, [])
                        self.assertTrue(pid_alive(child, created))
                        kill_tree(process)
                        process.wait(timeout=5)
                        deadline=time.monotonic()+5
                        while pid_alive(child, created) and time.monotonic()<deadline:
                            time.sleep(.1)
                        self.assertFalse(pid_alive(child, created))
                    finally:
                        kill_tree(process)
            self.assertEqual(sampler.seen, {})
