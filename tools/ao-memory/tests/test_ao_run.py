import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ao_state
from common import hidden_popen_kwargs, pid_alive, process_start_time, read_json

ROOT=Path(__file__).resolve().parents[1]
JOURNAL=ROOT.parent/'ao-journal'

class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.queue=self.base/'queue.json'; self.journal=self.base/'journal.jsonl'; self.jobs=self.base/'jobs'
        self.brief=self.base/'brief tiếng Việt.md'; self.brief.write_text('Làm việc',encoding='utf-8')
        self.output=self.base/'kết quả.txt'
        self.worker=self.base/'fake.py'
        self.worker.write_text('import sys,time\nfrom pathlib import Path\nmode=sys.argv[1]\nif mode=="sleep": time.sleep(8)\nif mode=="ok": Path(sys.argv[2]).write_text("ok")\nif mode=="secret": print("SECRET_TEST_KEY_123")\n',encoding='utf-8')
        self.cfg=self.base/'workers.json'; self.mode='ok'; self.write_cfg()
        self.addCleanup(self.wait_supervisors)

    def wait_supervisors(self):
        # Terminal status can precede the supervisor's final ledger checkpoint.
        # Keep its temporary files until it exits, rather than racing rmtree.
        for job in self.jobs.glob('*/job.json'):
            status=read_json(job.parent/'status.json', {})
            pid=status.get('supervisor_pid')
            created=status.get('supervisor_created')
            deadline=time.monotonic()+10
            while pid_alive(pid, created) and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertFalse(pid_alive(pid, created), 'test supervisor still running during cleanup')

    def write_cfg(self):
        def c(mode): return {'enabled':True,'kind':'fake','command':[sys.executable,str(self.worker),mode,str(self.output)],'brief_mode':'argument','cwd':str(ROOT),'env':{},'key_file':None,'key_var':None,'stall_seconds':0.8,'done_markers':[],'error_patterns':[]}
        self.cfg.write_text(json.dumps({'fake':c(self.mode),'backup':c('ok')}),encoding='utf-8')

    def call(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'ao_run.py'),'--queue',str(self.queue),'--journal',str(self.journal),'--workers',str(self.cfg),'--jobs',str(self.jobs),*args],cwd=ROOT,text=True,capture_output=True,encoding='utf-8',errors='replace',env={**os.environ,'PYTHONIOENCODING':'utf-8','AO_AUTOREPORT':'0'}, **hidden_popen_kwargs())

    def start(self,*extra):
        return self.call('start','--lane','A','--id','T','--title','thử','--workflow','test','--worker','fake','--brief',str(self.brief),'--signal','file:'+str(self.output),*extra)

    def wait(self,terminal=('DONE_NEEDS_REVIEW','GAVE_UP','SUPERVISOR_ERROR')):
        end=time.monotonic()+20
        while time.monotonic()<end:
            p=self.jobs/'T'/'status.json'
            if p.exists():
                x=read_json(p,{})
                if x.get('state') == 'SUPERVISOR_ERROR':
                    self.fail('SUPERVISOR_ERROR status=' + json.dumps(x, ensure_ascii=False) + '\nevents=' + (self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8'))
                if x.get('state') in terminal: return x
            time.sleep(.1)
        self.fail('supervisor timeout; events=' + (self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8'))

    def ledger(self):
        p=subprocess.run([sys.executable,str(JOURNAL/'queue.py'),'--queue',str(self.queue),'--journal',str(self.journal),'state','--json'],cwd=ROOT,text=True,capture_output=True,encoding='utf-8', **hidden_popen_kwargs())
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        return json.loads(p.stdout)['tasks'][0]

    def test_success_not_accepted(self):
        p=self.start(); self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('supervisor_pid=',p.stdout)
        self.assertEqual(self.wait()['state'],'DONE_NEEDS_REVIEW')
        self.assertEqual(self.ledger()['status'],'done')

    def lane_call(self, *extra):
        return self.call('start','--id','T','--title','thử','--workflow','test',
                         '--worker','fake','--brief',str(self.brief),
                         '--signal','file:'+str(self.output),*extra)

    def test_missing_lane_no_writes(self):
        before = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        result = self.lane_call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('THIẾU --lane', result.stderr)
        for label in ('A-G', 'A = nền AO', 'E = dự án lẻ'):
            self.assertIn(label, result.stderr)
        self.assertFalse(self.jobs.exists())
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.journal.exists())
        self.assertEqual(before, {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_invalid_lane_no_writes(self):
        for lane in ('Z', 'AB', ''):
            result = self.lane_call('--lane', lane)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--lane: chọn một chữ A-G', result.stderr)
            self.assertFalse(self.jobs.exists())
            self.assertFalse(self.queue.exists())
            self.assertFalse(self.journal.exists())

    def test_lowercase_lane_saved_and_status(self):
        result = self.lane_call('--lane', 'b')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_json(self.jobs/'T'/'job.json', {})['lane'], 'B')
        self.wait()
        self.assertEqual(json.loads(self.call('status','--id','T').stdout)['lane'], 'B')

    def test_maintenance_warns_without_blocking_start(self):
        import argparse
        import contextlib
        import io
        import ao_run
        sys.path.insert(0, str(ROOT.parent/'ao-report'))
        import maintenance
        flag = self.base/'maintenance_flag.json'
        maintenance.write({'on': True, 'at': '2026-10-12T08:00:00+00:00', 'why': 'thử'}, flag)
        args = argparse.Namespace(lane='A', id='T', title='thử', workflow='test', worker='fake',
            brief=str(self.brief), signal=['file:'+str(self.output)], fallback_worker=[], depends_on=[],
            max_retries=0, queue=self.queue, journal=self.journal, jobs=self.jobs, workers=self.cfg,
            project=None, data_class='internal', work_type='simple', requires_owner_approval=False,
            required_metric=[])
        err = io.StringIO()
        with patch.dict(os.environ, {'AO_AUTOREPORT':'0'}), patch.object(maintenance, 'PATH', flag), \
             patch.object(ao_run, 'launch_supervisor', return_value=123) as launch, \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            ao_run.start(args)
        launch.assert_called_once()
        self.assertTrue((self.jobs/'T'/'job.json').exists())
        self.assertIn('ĐANG BẢO TRÌ làn A', err.getvalue())

    def test_failure_retries_and_gives_up(self):
        self.mode='fail'; self.write_cfg()
        self.assertEqual(self.start('--max-retries','2').returncode,0)
        self.assertEqual(self.wait()['state'],'GAVE_UP')
        rows=[json.loads(x) for x in self.journal.read_text(encoding='utf-8').splitlines()]
        attempts=[x for x in rows if x.get('status')=='dispatched']
        self.assertEqual([x['attempt_id'] for x in attempts],['T#1','T#2','T#3'])
        self.assertTrue(all(x.get('retry_reason') or x.get('reason') for x in attempts[1:]))

    def test_log_patterns_do_not_override_success(self):
        self.worker.write_text('import sys\nfrom pathlib import Path\nfor _ in range(3): print("error: quoted 429", flush=True)\nPath(sys.argv[2]).write_text("ok")\nprint("tokens used", flush=True)\n',encoding='utf-8')
        cfg=json.loads(self.cfg.read_text(encoding='utf-8'))
        cfg['fake']['error_patterns']=['error:', '429']
        cfg['fake']['done_markers']=['tokens used']
        self.cfg.write_text(json.dumps(cfg),encoding='utf-8')
        self.assertEqual(self.start('--max-retries','2','--fallback-worker','backup').returncode,0)
        final=self.wait()
        self.assertEqual(final['state'],'DONE_NEEDS_REVIEW')
        self.assertEqual(final['retries'],0)
        events=[json.loads(x) for x in (self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([x['type'] for x in events],['START','DONE_NEEDS_REVIEW'])
        self.assertEqual(self.ledger()['status'],'done')

    def test_signals_override_missing_done_marker(self):
        self.worker.write_text('import sys,time\nfrom pathlib import Path\nprint("error: quoted 429", flush=True)\ntime.sleep(0.2)\nPath(sys.argv[2]).write_text("ok")\n',encoding='utf-8')
        cfg=json.loads(self.cfg.read_text(encoding='utf-8'))
        cfg['fake']['error_patterns']=['error:', '429']
        cfg['fake']['done_markers']=['tokens used']
        self.cfg.write_text(json.dumps(cfg),encoding='utf-8')
        self.assertEqual(self.start('--max-retries','2','--fallback-worker','backup').returncode,0)
        self.assertEqual(self.wait()['state'],'DONE_NEEDS_REVIEW')
        events=[json.loads(x) for x in (self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([x['type'] for x in events],['START','DONE_NEEDS_REVIEW'])
        self.assertEqual(self.ledger()['status'],'done')

    def test_missing_output_retries_and_gives_up_with_hint(self):
        self.mode='fail'; self.write_cfg()
        cfg=json.loads(self.cfg.read_text(encoding='utf-8'))
        for worker in cfg.values():
            worker['command']=[sys.executable,'-c','print("API Error: 429")']
            worker['error_patterns']=['API Error']
        self.cfg.write_text(json.dumps(cfg),encoding='utf-8')
        self.assertEqual(self.start('--max-retries','2','--fallback-worker','backup').returncode,0)
        self.assertEqual(self.wait()['state'],'GAVE_UP')
        events=[json.loads(x) for x in (self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len([x for x in events if x['type']=='FAILED']),3)
        self.assertEqual(len([x for x in events if x['type']=='RETRY']),2)
        self.assertIn('worker=backup',str(events))
        self.assertIn('exit=0',events[-2]['detail'])
        self.assertIn('file:',events[-2]['detail'])
        self.assertIn('API Error: 429',events[-2]['detail'])
        self.assertIn('dòng log:',events[-2]['detail'])

    def test_nonzero_exit_reason(self):
        cfg=json.loads(self.cfg.read_text(encoding='utf-8'))
        cfg['fake']['command']=[sys.executable,'-c',f'from pathlib import Path; Path({str(self.output)!r}).write_text("ok"); raise SystemExit(7)']
        self.cfg.write_text(json.dumps(cfg),encoding='utf-8')
        self.assertEqual(self.start('--max-retries','0').returncode,0)
        self.assertEqual(self.wait()['state'],'GAVE_UP')
        self.assertTrue(self.output.is_file())
        self.assertIn('exit=7',(self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8'))

    def test_stall(self):
        self.mode='sleep'; self.write_cfg()
        self.assertEqual(self.start('--max-retries','1').returncode,0)
        self.assertEqual(self.wait()['state'],'GAVE_UP')
        self.assertIn('STALL',(self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8'))

    def test_fallback(self):
        self.mode='fail'; self.write_cfg()
        self.assertEqual(self.start('--fallback-worker','backup').returncode,0)
        self.assertEqual(self.wait()['state'],'DONE_NEEDS_REVIEW')
        rows=[json.loads(x) for x in self.journal.read_text(encoding='utf-8').splitlines()]
        dispatched=[x for x in rows if x.get('status')=='dispatched']
        self.assertEqual(dispatched[1]['worker_switched_from'],'fake')

    def test_validation(self):
        bad=self.call('start','--lane','A','--id','T','--title','x','--workflow','w','--worker','fake','--brief',str(self.base/'missing'),'--signal','file:'+str(self.output))
        self.assertNotEqual(bad.returncode,0); self.assertIn('brief',bad.stderr)
        self.assertFalse(self.queue.exists())
        bad=self.start('--signal','invalid')
        self.assertNotEqual(bad.returncode,0); self.assertFalse(self.queue.exists())
        self.assertEqual(self.start().returncode,0)
        dup=self.start(); self.assertNotEqual(dup.returncode,0); self.assertIn('id đã tồn tại',dup.stderr)
        self.wait()

    def test_bad_ledger_no_worker(self):
        self.queue=self.base
        p=self.start(); self.assertNotEqual(p.returncode,0)
        self.assertFalse((self.jobs/'T').exists())

    def test_detached_after_start_exits(self):
        self.mode='sleep'; self.write_cfg()
        p=self.start('--max-retries','0'); self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('supervisor_pid=',p.stdout)
        self.assertEqual(self.wait()['state'],'GAVE_UP')

    def test_resume_orphan(self):
        self.mode='sleep'; self.write_cfg()
        self.assertEqual(self.start('--max-retries','1').returncode,0)
        path=self.jobs/'T'/'status.json'
        for _ in range(50):
            if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('state')=='RUNNING': break
            time.sleep(.1)
        x=json.loads(path.read_text(encoding='utf-8'))
        subprocess.run(['taskkill','/F','/PID',str(x['supervisor_pid'])],capture_output=True, **hidden_popen_kwargs())
        denied=self.call('resume','--id','T'); self.assertEqual(denied.returncode,2,denied.stderr)
        # Recovery must not create a duplicate while orphan worker is alive.
        blocked=self.call('resume','--id','T','--recover-delegation','--recovery-reason','fixture supervisor killed'); self.assertEqual(blocked.returncode,2,blocked.stderr)
        subprocess.run(['taskkill','/F','/PID',str(x['worker_pid'])],capture_output=True, **hidden_popen_kwargs())
        jobpath = self.jobs/'T'/'job.json'
        legacyjob = read_json(jobpath, {})
        legacyjob.pop('lane', None)
        jobpath.write_text(json.dumps(legacyjob), encoding='utf-8')
        p=self.call('resume','--id','T','--recover-delegation','--recovery-reason','fixture supervisor and worker terminated'); self.assertEqual(p.returncode,0,p.stderr)
        self.wait()
        events=(self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8')
        self.assertIn('ORPHANED',events)

    def test_secret_redacted(self):
        key=self.base/'key.env'; key.write_text('FAKE_KEY=SECRET_TEST_KEY_123',encoding='utf-8')
        cfg=json.loads(self.cfg.read_text(encoding='utf-8'))
        cfg['fake']['command']=[sys.executable,str(self.worker),'secret',str(self.output)]
        cfg['fake']['key_file']=str(key); cfg['fake']['key_var']='FAKE_KEY'
        self.cfg.write_text(json.dumps(cfg),encoding='utf-8')
        self.assertEqual(self.start('--max-retries','0').returncode,0)
        self.wait()
        for file in (self.jobs/'T').rglob('*'):
            if file.is_file(): self.assertNotIn('SECRET_TEST_KEY_123',file.read_text(encoding='utf-8'))

    def fake_status(self, ident, supervisor_pid, worker_pid=99999999):
        path=self.jobs/ident; path.mkdir(parents=True,exist_ok=True)
        row={'id':ident,'state':'RUNNING','supervisor_pid':supervisor_pid,
             'worker_pid':worker_pid,'last_log_change':'2026-10-02T00:00:00+00:00'}
        if supervisor_pid != 99999999: row['supervisor_created']=process_start_time(supervisor_pid)
        (path/'status.json').write_text(json.dumps(row),encoding='utf-8')
        (path/'events.jsonl').touch()
        return path

    def test_effective_status_dead_and_live(self):
        dead=self.fake_status('DEAD',99999999)
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],cwd=self.base, **hidden_popen_kwargs())
        self.addCleanup(lambda: (child.terminate(),child.wait(timeout=5)) if child.poll() is None else None)
        self.fake_status('LIVE',child.pid)
        rows=[json.loads(line) for line in self.call('list').stdout.splitlines()]
        by_id={row['id']:row for row in rows}
        self.assertEqual(by_id['DEAD']['state'],'MẤT_GIÁM_SÁT')
        self.assertEqual(by_id['DEAD']['lane'], '?')
        self.assertFalse(by_id['DEAD']['supervisor_alive'])
        self.assertFalse(by_id['DEAD']['worker_alive'])
        self.assertIsInstance(by_id['DEAD']['log_age_minutes'],float)
        self.assertEqual(by_id['LIVE']['state'],'RUNNING')
        self.assertTrue(by_id['LIVE']['supervisor_alive'])
        self.assertEqual(json.loads((dead/'status.json').read_text())['state'],'RUNNING')

    def test_reap_only_selected_orphans(self):
        dead=self.fake_status('DEAD',99999999)
        other=self.fake_status('OTHER',99999999)
        p=self.call('reap','--id','DEAD'); self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads((dead/'status.json').read_text())['state'],'ORPHANED')
        self.assertIn('ORPHANED',(dead/'events.jsonl').read_text())
        self.assertEqual(json.loads((other/'status.json').read_text())['state'],'RUNNING')
        self.assertEqual((other/'events.jsonl').read_text(),'')
        self.assertFalse(self.journal.exists())

    def test_state_warns_dead_supervisor(self):
        self.fake_status('DEAD',99999999)
        ledger={'tasks':[{'id':'DEAD','title':'thử','status':'dispatched'}]}
        with patch.object(ao_state,'HERE',self.base),patch.object(ao_state.threads,'pending',return_value=[]):
            out=ao_state.generate(ledger=ledger,projects=[],decisions=[],rules='',taskboard={},events=[])
        warning='\n'.join(out['sections']['CẢNH BÁO'])
        self.assertIn('DEAD: giám sát mất từ',warning)
        self.assertIn('ao_run.py resume --id DEAD',warning)
        self.assertIn('reap --id DEAD',warning)

    def write_wait_status(self, state, retries=0, max_retries=0):
        path=self.jobs/'WAIT'; path.mkdir(parents=True,exist_ok=True)
        (path/'status.json').write_text(json.dumps({'id':'WAIT','state':state,'attempt_id':'WAIT#1',
            'worker':'fake','retries':retries,'supervisor_pid':99999999}),encoding='utf-8')
        (path/'job.json').write_text(json.dumps({'max_retries':max_retries}),encoding='utf-8')
        return path

    def test_wait_terminal_states(self):
        for state, expected, code in [('DONE_NEEDS_REVIEW','DONE_NEEDS_REVIEW',0),
            ('GAVE_UP','FAILED',10),('FAILED','FAILED',10),('STOPPED','STOPPED',11),
            ('ORPHANED','ORPHANED',12),('SUPERVISOR_ERROR','FAILED',10)]:
            with self.subTest(state=state):
                self.write_wait_status(state)
                p=self.call('wait','--id','WAIT','--timeout','0.1','--poll','0.01')
                self.assertEqual(p.returncode,code,p.stderr)
                self.assertEqual(len(p.stdout.splitlines()),1)
                row=json.loads(p.stdout)
                self.assertEqual(row['state'],expected)
                self.assertEqual(row['id'],'WAIT')
                self.assertEqual(row['attempt_id'],'WAIT#1')
                self.assertEqual(row['worker'],'fake')
                self.assertEqual(row['retries'],0)
                self.assertIsInstance(row['waited_seconds'],float)
                self.assertTrue(row['next'])

    def test_wait_retry_then_done(self):
        path=self.write_wait_status('FAILED',retries=0,max_retries=1)
        (path/'events.jsonl').write_text('\n'.join(json.dumps({'type':kind}) for kind in
            ('FAILED','RETRY','START'))+'\n',encoding='utf-8')
        def finish():
            time.sleep(.15)
            row=json.loads((path/'status.json').read_text(encoding='utf-8'))
            row.update(state='DONE_NEEDS_REVIEW',attempt_id='WAIT#2',retries=1)
            (path/'status.json').write_text(json.dumps(row),encoding='utf-8')
        import threading
        thread=threading.Thread(target=finish); thread.start()
        try: p=self.call('wait','--id','WAIT','--timeout','2','--poll','0.02')
        finally: thread.join()
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)['attempt_id'],'WAIT#2')

    def test_wait_timeout_and_missing_id(self):
        self.write_wait_status('FAILED',retries=0,max_retries=1)
        p=self.call('wait','--id','WAIT','--timeout','0.08','--poll','0.01')
        self.assertEqual(p.returncode,13,p.stderr)
        self.assertEqual(json.loads(p.stdout)['state'],'TIMEOUT')
        p=self.call('wait','--id','MISSING','--timeout','0')
        self.assertEqual(p.returncode,2)
        self.assertEqual(json.loads(p.stdout)['state'],'NOT_FOUND')

    def test_wait_dead_supervisor(self):
        self.fake_status('DEAD',99999999)
        p=self.call('wait','--id','DEAD','--timeout','0.1','--poll','0.01')
        self.assertEqual(p.returncode,12,p.stderr)
        self.assertEqual(json.loads(p.stdout)['state'],'MẤT_GIÁM_SÁT')

class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        import common
        self.common = common
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.base)}, clear=True)
        self.environment.start(); self.addCleanup(self.environment.stop)

    def candidate(self, name, version, legacy=False):
        exe = self.base / ('Programs/OpenAI/Codex/bin/codex.exe' if legacy else f'OpenAI/Codex/bin/{name}/codex.exe')
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(version, encoding='utf-8')
        return exe

    def run_fake(self, args, **kwargs):
        # Real Python subprocesses exercise success, exit errors and timeout handling.
        content = Path(args[0]).read_text(encoding='utf-8')
        code = 'import time; time.sleep(2)' if content == 'timeout' else 'raise SystemExit(7)' if content == 'broken' else f'print("codex-cli {content}")'
        return self.real_run([sys.executable, '-c', code], **kwargs)

    def discover(self):
        self.real_run = subprocess.run
        with patch('subprocess.run', side_effect=self.run_fake):
            return self.common.discover_codex()

    def test_numeric_and_prerelease_order(self):
        for name, version in [('old','0.99.0'),('a','0.159.0-alpha.2.1'),('b','0.159.0-alpha.12.1'),('beta','0.159.0-beta.1'),('rc','0.159.0-rc.1')]:
            self.candidate(name, version)
        self.assertEqual(self.discover()[1], '0.159.0-rc.1')
        official = self.candidate('legacy', '0.159.0', legacy=True)
        self.assertEqual(self.discover()[:2], (str(official), '0.159.0'))
        official.unlink()
        (self.base/'OpenAI/Codex/bin/rc/codex.exe').unlink()
        (self.base/'OpenAI/Codex/bin/beta/codex.exe').unlink()
        self.assertEqual(self.discover()[1], '0.159.0-alpha.12.1')

    def test_broken_timeout_and_invalid_skipped(self):
        self.candidate('bad','broken'); self.candidate('slow','timeout'); self.candidate('invalid','garbage')
        good = self.candidate('good','0.159.0')
        original = self.common.probe_codex
        with patch.object(self.common, 'probe_codex', side_effect=lambda exe: original(exe, timeout=.3)):
            self.assertEqual(self.discover()[:2], (str(good), '0.159.0'))

    def test_no_candidates_clear_error(self):
        with self.assertRaisesRegex(ValueError, 'Không tìm được Codex'):
            self.discover()

    def test_override_and_invalid_override(self):
        self.candidate('new','0.159.0')
        pinned = self.candidate('old','0.99.0')
        os.environ['AO_CODEX_EXE'] = str(pinned)
        self.assertEqual(self.discover(), (str(pinned), '0.99.0', [(str(pinned), '0.99.0')]))
        os.environ['AO_CODEX_EXE'] = str(self.base/'missing.exe')
        with self.assertRaisesRegex(ValueError, 'AO_CODEX_EXE không chạy được'): self.discover()


class CodexRunTests(RunTests):
    # Only new integration cases; inherited suite runs once in RunTests.
    def test_doctor_and_start_metadata(self):
        self.output = self.base/'output.txt'
        fake = self.base/'codex.bat'
        fake.write_text('@echo off\nif "%~1"=="--version" (echo codex-cli 0.159.0-alpha.12.1 & exit /b 0)\n"' + sys.executable + '" "' + str(self.worker) + '" ok "' + str(self.output) + '"\n', encoding='utf-8')
        cfg = json.loads(self.cfg.read_text(encoding='utf-8'))
        cfg['fake']['command'] = ['{codex_latest}']
        self.cfg.write_text(json.dumps(cfg), encoding='utf-8')
        with patch.dict(os.environ, {'AO_CODEX_EXE': str(fake)}):
            doctor = self.call('doctor')
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn(str(fake), doctor.stdout)
            self.assertIn('version=0.159.0-alpha.12.1', doctor.stdout)
            self.assertIn('CẢNH BÁO', doctor.stdout)
            self.assertIn('command=True', doctor.stdout)
            started = self.start('--max-retries','0')
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(self.wait()['state'], 'DONE_NEEDS_REVIEW')
        rows = [json.loads(line) for line in (self.jobs/'T'/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        start = next(row for row in rows if row['type'] == 'START')
        self.assertIn('exe='+str(fake), start['detail'])
        self.assertIn('version=0.159.0-alpha.12.1', start['detail'])

# Avoid collecting inherited tests a second time.
for _name in list(vars(RunTests)):
    if _name.startswith('test_'): setattr(CodexRunTests, _name, None)

if __name__=='__main__': unittest.main()
