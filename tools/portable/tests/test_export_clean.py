from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('exporter',Path(__file__).resolve().parents[1]/'export_clean.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ExportTests(unittest.TestCase):
    def make(self, root, name, content='abc'):
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content,encoding='utf-8');return p

    def test_no_git_nested_rules_and_state(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'src';root.mkdir();dest=Path(t)/'out'
            self.make(root,'.gitignore','*.draft\n/hidden/\n!keep.draft\n**/cache/\n')
            self.make(root,'sub/.gitignore','*.txt\n!keep.txt\n')
            for name in ['ok.py','drop.draft','keep.draft','hidden/x','sub/a.txt','sub/keep.txt','sub/cache/x','tools/ao-memory/data/toolbox.json','tools/ao-journal/journal.jsonl','burn/x','memory/x','x/__pycache__/x','nested/jobs/x']:
                self.make(root,name)
            count,size=m.export_clean(root,dest)
            names={p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file()}
            self.assertEqual(names,{'.gitignore','sub/.gitignore','ok.py','keep.draft','sub/keep.txt'})
            self.assertEqual(count,len(names));self.assertEqual(size,sum(p.stat().st_size for p in dest.rglob('*') if p.is_file()))
            self.assertTrue((root/'tools/ao-memory/data/toolbox.json').is_file())
            with self.assertRaises(ValueError):m.export_clean(root,dest)
            with self.assertRaises(ValueError):m.export_clean(root,root/'out')

    @unittest.skipUnless(shutil.which('git'),'git unavailable')
    def test_git_and_fallback_agree_with_ignore_semantics(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'src';root.mkdir()
            def git(*args):
                return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,creationflags=0x08000000 if os.name=='nt' else 0).stdout
            git('init','-q')
            self.make(root,'.gitignore','*.draft\n!keep.draft\n/root-only\nignored/\n!ignored/a.py\n**/cache/\nescaped\\ name\n\\#hash\n')
            self.make(root,'sub/.gitignore','*.txt\n!keep.txt\n')
            for name in ['a.py','keep.draft','drop.draft','root-only','sub/root-only','ignored/a.py','sub/keep.txt','sub/drop.txt','sub/cache/z','escaped name','#hash']:
                self.make(root,name)
            self.assertEqual(set(m.repository_files(root)),set(m.fallback_files(root)))
            self.make(root,'tracked.draft');git('add','-f','tracked.draft')
            self.make(root,'tools/ao-journal/queue.json','[1]');git('add','-f','tools/ao-journal/queue.json')
            files=m.repository_files(root)
            self.assertIn(Path('tracked.draft'),files) # tracked ignored source is part of Git export
            self.assertNotIn(Path('tools/ao-journal/queue.json'),files)
            count,size=m.export_clean(root,Path(t)/'out')
            self.assertGreater(count,0);self.assertGreater(size,0)

    def test_existing_empty_target_and_nonempty_refusal(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'src';root.mkdir();self.make(root,'a')
            dest=Path(t)/'out';dest.mkdir();self.assertEqual(m.export_clean(root,dest),(1,3))
            with self.assertRaises(ValueError):m.export_clean(root,dest)
            self.assertEqual((dest/'a').read_text(),'abc')

    def test_scanner_sees_ignored_cache(self):
        scan_spec=importlib.util.spec_from_file_location('scan',Path(__file__).resolve().parents[1]/'scan_paths.py')
        scanner=importlib.util.module_from_spec(scan_spec);scan_spec.loader.exec_module(scanner)
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.make(root,'.gitignore','data/\n')
            self.make(root,'data/toolbox.json',str(Path.home()))
            self.assertEqual(scanner.scan(root),[('data/toolbox.json',1,'machine-path')])

class BusinessScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('business_scan',Path(__file__).resolve().parents[1]/'scan_paths.py')
        cls.scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.scanner)

    def test_case_accent_and_identifier_variants(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'repo';root.mkdir()
            terms_file=Path(t)/'terms.txt'
            terms_file.write_text('từ khóa mẫu 1\n',encoding='utf-8')
            variants=['TỪ KHÓA MẪU 1','tu khoa mau 1','tu-khoa-mau-1',
                      'tukhoamau1','tu_khoa_mau_1']
            (root/'sample.txt').write_text('\n'.join(variants),encoding='utf-8')
            with patch.dict(os.environ,{'AO_SENSITIVE_TERMS_FILE':str(terms_file)}):
                hits=self.scanner.scan(root)
            self.assertEqual(len(hits),len(variants))
            self.assertTrue(all(h[2]=='business-name' for h in hits))

    def test_detector_has_no_business_whitelist(self):
        self.assertFalse(self.scanner.allowed('tools/portable/scan_paths.py',
                                             'tukhoa_mau_1','business-name'))
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'repo';root.mkdir()
            terms_file=Path(t)/'terms.txt';terms_file.write_text('tukhoa_mau_1',encoding='utf-8')
            p=root/'tools/portable/scan_paths.py';p.parent.mkdir(parents=True)
            p.write_text('# tukhoa_mau_1',encoding='utf-8')
            with patch.dict(os.environ,{'AO_SENSITIVE_TERMS_FILE':str(terms_file)}):
                self.assertEqual(self.scanner.scan(root),
                                 [('tools/portable/scan_paths.py',1,'business-name')])

    def test_ignored_state_business_only(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'repo';root.mkdir()
            terms_file=Path(t)/'terms.txt';terms_file.write_text('tukhoa_mau_1',encoding='utf-8')
            (root/'.gitignore').write_text('data/\n',encoding='utf-8')
            p=root/'data/state.txt';p.parent.mkdir();p.write_text('tukhoa_mau_1',encoding='utf-8')
            with patch.dict(os.environ,{'AO_SENSITIVE_TERMS_FILE':str(terms_file)}):
                self.assertEqual(self.scanner.scan(root),[])

    def test_unconfigured_and_missing_file_cli(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'repo';root.mkdir()
            script=Path(__file__).resolve().parents[1]/'scan_paths.py'
            for configured in (None,str(Path(t)/'missing.txt')):
                env=dict(os.environ,PYTHONUTF8='1')
                env.pop('AO_SENSITIVE_TERMS_FILE',None)
                if configured:env['AO_SENSITIVE_TERMS_FILE']=configured
                result=subprocess.run([__import__('sys').executable,str(script),'--root',str(root)],
                                      env=env,cwd=root,capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('chưa cấu hình danh sách từ nhạy cảm — chưa quét tên kinh doanh',result.stdout)
                self.assertIn('SCAN_PATHS: 0',result.stdout)

    def test_missing_terms_still_checks_paths_and_secrets(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'sample.txt').write_text(str(Path.home())+'\n'+'s'+'k-fakevalue123',encoding='utf-8')
            with patch.dict(os.environ,{'AO_SENSITIVE_TERMS_FILE':str(root/'missing.txt')}):
                self.assertEqual(self.scanner.scan(root),
                                 [('sample.txt',1,'machine-path'),('sample.txt',2,'secret')])

    def test_bom_blank_lines_and_unreadable_terms(self):
        with tempfile.TemporaryDirectory() as t:
            terms_file=Path(t)/'terms.txt'
            terms_file.write_text('\ufeff  tukhoa_mau_1  \n\n',encoding='utf-8')
            with patch.dict(os.environ,{'AO_SENSITIVE_TERMS_FILE':str(terms_file)}):
                self.assertEqual(self.scanner.load_business_terms(),('tukhoa_mau_1',))
                terms_file.write_bytes(bytes([255,254,255]))
                self.assertEqual(self.scanner.load_business_terms(),())

if __name__=='__main__':unittest.main()
