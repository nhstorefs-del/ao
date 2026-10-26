"""Portable contracts exercised without installed workers or provider calls."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('portable_scan',REPO/'tools/portable/scan_paths.py')
scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(scanner)

class PortableTests(unittest.TestCase):
    def test_scanner_detects_paths_and_secret_without_exposing_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); secret='s'+'k-PORTABLE_TEST_VALUE'
            (root/'probe.py').write_text(str(Path.home())+'\n'+secret,encoding='utf-8')
            rows=scanner.scan(root)
            self.assertEqual({r[2] for r in rows},{'machine-path','secret'})
            self.assertNotIn(secret,json.dumps(rows))

    def test_whitelist_cannot_hide_second_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);f=root/'tools/ao-memory/tests/test_ao_memory.py';f.parent.mkdir(parents=True)
            f.write_text('s'+'k-SECRET123\n',encoding='utf-8')
            self.assertEqual(scanner.scan(root),[])
            f.write_text('s'+'k-SECRET123 s'+'k-ANOTHER_VALUE\n',encoding='utf-8')
            self.assertEqual(len(scanner.scan(root)),1)

    def test_override_and_boot_with_no_optional_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo with spaces';memory=Path(temp)/'notes';root.mkdir()
            # Only actual dependency modules, no history or machine state.
            for name in ['journal.py','queue.py','file_lock.py','check_completion.py','delegation_gate.py']:
                dest=root/'tools/ao-journal'/name;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(REPO/'tools/ao-journal'/name,dest)
            for name in ['common.py','ao_run.py','workers.example.json']:
                dest=root/'tools/ao-memory'/name.replace('.example','');dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(REPO/'tools/ao-memory'/name,dest)
            env=dict(os.environ,AO_ROOT=str(root),AO_MEMORY_DIR=str(memory),AO_SECRETS_DIR=str(Path(temp)/'no-secrets'),PATH='',LOCALAPPDATA=str(Path(temp)/'no-cli'),PYTHONUTF8='1')
            for variable in ['AO_CODEX_EXE','AO_QWEN_EXE','AO_CODEX2_HOME']:env.pop(variable,None)
            def run(script,args):
                return subprocess.run([sys.executable,str(script),*args],cwd=Path(temp),env=env,capture_output=True,text=True,encoding='utf-8',creationflags=0x08000000 if os.name=='nt' else 0)
            p=run(REPO/'tools/portable/setup_ao.py',[])
            self.assertEqual(p.returncode,0,p.stderr);self.assertIn('VERIFY PASS: 0',p.stdout)
            self.assertTrue(memory.is_dir())
            p=run(root/'tools/ao-memory/ao_run.py',['doctor'])
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertIn('command=False',p.stdout)
            self.assertIn('codex-main: không sẵn sàng',p.stdout)
            self.assertIn('AO_CODEX2_HOME',p.stdout)
            self.assertEqual((root/'tools/ao-journal/journal.jsonl').stat().st_size,0)

    def test_missing_memory_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            env=dict(os.environ,AO_MEMORY_DIR=str(Path(temp)/'missing'),PYTHONUTF8='1')
            p=subprocess.run([sys.executable,str(REPO/'tools/ao-memory/ao_recall.py'),'example','--in','memory','--json'],cwd=Path(temp),env=env,capture_output=True,text=True,encoding='utf-8',creationflags=0x08000000 if os.name=='nt' else 0)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(json.loads(p.stdout),{'memory':[]})
            self.assertIn('Không có bộ nhớ',p.stderr)

if __name__=='__main__':unittest.main()
