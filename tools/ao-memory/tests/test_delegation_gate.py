import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT.parent/'ao-journal'))
import delegation_gate as gate
import journal
import ao_run
import ao_supervisor
import check_completion
class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.ledger=self.root/'journal.jsonl'
        self.brief=self.root/'brief.md'; self.brief.write_text('implement test artifact')
    def prepare(self): return gate.prepare(self.ledger,'T#1','worker',self.brief)
    def receipt(self): return journal.append('T','worker',self.brief,['file:'+str(self.root/'out')],self.ledger)
    def test_required_states(self):
        self.prepare(); self.assertEqual([r['state'] for r in gate.snapshot(self.ledger,'T#1')],['RECEIVED','DELEGATION_DECISION_REQUIRED','DELEGATED'])
        gate.claim(self.ledger,'T#1','worker',self.brief); gate.finish(self.ledger,'T#1','worker',self.brief)
        self.assertEqual(gate.snapshot(self.ledger,'T#1')[-1]['state'],'FINISHED')
    def test_cli_self_execution_denied(self):
        proc=subprocess.run([sys.executable,str(ROOT.parent/'ao-journal/delegation_gate.py'),'--journal',str(self.ledger),'supervisor-action','--id','S','--reason','coordination','--operation','write_code'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,2);self.assertIn('SELF_EXECUTION_OPERATION_DENIED',proc.stdout)
        self.assertFalse(gate.database(self.ledger).exists())
    def test_missing_state_no_execution(self):
        with self.assertRaises(gate.GateRefused): gate.claim(self.ledger,'T#1','worker',self.brief)
    def test_skipped_state(self):
        def attack(db):
            gate.write(db,'T#1','RECEIVED'); gate.write(db,'T#1','EXECUTING')
        with self.assertRaises(gate.GateRefused): gate.transaction(self.ledger,attack,True)
        self.assertEqual(gate.snapshot(self.ledger,'T#1'),[])
    def test_invalid_self_reason(self):
        for reason in ('urgent','owner_said','coordination'):
            with self.assertRaises(gate.GateRefused): gate.supervisor_action(self.ledger,'S',reason,'write_code')
    def test_self_worker_identity_denied(self):
        for worker in ('self','supervisor','coordinator','codex-fallback','ao-run'):
            with self.assertRaises(gate.GateRefused): gate.prepare(self.ledger,'T#1',worker,self.brief)
    def test_allowlisted_operations(self):
        value=gate.supervisor_action(self.ledger,'S','independent_verification','hash_artifact',self.brief)
        self.assertEqual(len(value['sha256']),64)
        self.assertEqual([r['state'] for r in gate.snapshot(self.ledger,'S')],['RECEIVED','DELEGATION_DECISION_REQUIRED','SELF_EXECUTION_AUTHORIZED','EXECUTING','FINISHED'])
    def test_decision_durable_before_handler(self):
        original=Path.open
        def observed(path,*args,**kw):
            if path==self.brief and args and args[0]=='rb':
                self.assertEqual(gate.snapshot(self.ledger,'S')[-1]['state'],'EXECUTING')
            return original(path,*args,**kw)
        with patch.object(Path,'open',observed): gate.supervisor_action(self.ledger,'S','independent_verification','hash_artifact',self.brief)
    def test_bounds(self):
        p=self.root/'huge'; p.write_bytes(b'x'*(16*1024*1024+1))
        with self.assertRaises(gate.GateRefused): gate.supervisor_action(self.ledger,'S','independent_verification','hash_artifact',p)
    def test_secret_path(self):
        for p in (self.root/'.env',self.root/'.worker-keys/key',self.root/'.ENV',self.root/'.WORKER-KEYS/key'):
            with self.assertRaises(gate.GateRefused): gate.supervisor_action(self.ledger,'S','trivial_unblocking','check_path',p)
    def test_double_claim_concurrent(self):
        self.prepare()
        def take(_):
            try: gate.claim(self.ledger,'T#1','worker',self.brief); return True
            except gate.GateRefused: return False
        with ThreadPoolExecutor(max_workers=8) as pool: result=list(pool.map(take,range(8)))
        self.assertEqual(sum(result),1)
    def test_replay(self):
        self.prepare(); gate.claim(self.ledger,'T#1','worker',self.brief); gate.finish(self.ledger,'T#1','worker',self.brief)
        with self.assertRaises(gate.GateRefused): gate.prepare(self.ledger,'T#1','worker',self.brief)
    def test_brief_worker_attempt_mismatch(self):
        self.prepare(); self.brief.write_text('changed')
        for attempt,worker in [('T#1','worker'),('T#2','worker'),('T#1','other')]:
            with self.assertRaises(gate.GateRefused): gate.claim(self.ledger,attempt,worker,self.brief)
    def test_sqlite_tamper(self):
        self.prepare()
        db=sqlite3.connect(gate.database(self.ledger))
        db.execute("UPDATE events SET payload='{}' WHERE seq=3"); db.commit(); db.close()
        with self.assertRaises(gate.GateRefused): gate.claim(self.ledger,'T#1','worker',self.brief)
    def test_deleted_state_fail_closed(self):
        self.prepare(); gate.database(self.ledger).unlink()
        with self.assertRaises(gate.GateRefused): gate.require(self.ledger,'T#1','worker',self.brief)
    def test_missing_state_cannot_be_recreated_for_recorded_attempt(self):
        self.receipt();gate.database(self.ledger).unlink()
        with self.assertRaises(gate.GateRefused): self.prepare()
    def test_direct_recovery_unknown_child_denied(self):
        self.prepare();gate.claim(self.ledger,'T#1','worker',self.brief)
        with patch.object(gate,'owner_dead',return_value=True):
            with self.assertRaisesRegex(gate.GateRefused,'CHILD_NOT_PROVEN_DEAD'): gate.recover(self.ledger,'T#1','worker',self.brief,'owner exited')
    def test_unknown_process_identity_cannot_execute(self):
        self.prepare()
        with patch.object(gate,'process_identity',return_value=None):
            gate.claim(self.ledger,'T#1','worker',self.brief)
            with self.assertRaises(gate.GateRefused): gate.require(self.ledger,'T#1','worker',self.brief,('EXECUTING',))
    def test_reserved_fields(self):
        with self.assertRaises(gate.GateRefused): gate.transaction(self.ledger,lambda db:gate.write(db,'T','RECEIVED',pid=999),True)
    def test_live_owner_cannot_recover(self):
        self.prepare(); gate.claim(self.ledger,'T#1','worker',self.brief)
        with self.assertRaises(gate.GateRefused): gate.recover(self.ledger,'T#1','worker',self.brief,'restart')
    def test_final_boundary_requires_worker_role(self):
        gate.supervisor_action(self.ledger,'S','coordination','inspect_state')
        with self.assertRaises(gate.GateRefused): gate.require(self.ledger,'S','worker',self.brief,('EXECUTING',))
    def test_direct_journal_api_records_gate(self):
        self.receipt(); gate.receipt_check(self.ledger,'T#1','worker',self.brief)
    def test_receipt_cannot_be_forged(self):
        self.prepare()
        with self.assertRaises(gate.GateRefused): gate.receipt_check(self.ledger,'T#1','worker',self.brief)
    def test_terminal_receipt_cannot_relaunch(self):
        self.receipt();journal.complete('T','done',True,journal_path=self.ledger,attempt_id='T#1')
        with self.assertRaisesRegex(gate.GateRefused,'TERMINAL_OR_STALE'): gate.receipt_check(self.ledger,'T#1','worker',self.brief)
    def test_direct_launch_legacy_no_side_effect(self):
        jobdir=self.root/'job'; jobdir.mkdir(); (jobdir/'job.json').write_text(json.dumps({'id':'T','attempt_id':'T#1','worker':'worker','brief':str(self.brief),'journal':str(self.ledger)}))
        with patch.object(ao_run.subprocess,'Popen') as popen:
            with self.assertRaises(gate.GateRefused): ao_run.launch_supervisor(jobdir)
            popen.assert_not_called()
    def test_direct_supervisor_legacy_before_config(self):
        jobdir=self.root/'job'; jobdir.mkdir(); (jobdir/'job.json').write_text(json.dumps({'id':'T','attempt_id':'T#1','worker':'worker','brief':str(self.brief),'journal':str(self.ledger)}))
        with patch.object(ao_supervisor,'key_value') as key, patch.object(ao_supervisor.subprocess,'Popen') as popen:
            with self.assertRaises(gate.GateRefused): ao_supervisor.run(jobdir)
            key.assert_not_called(); popen.assert_not_called()
    def test_shell_signals_never_execute(self):
        marker=self.root/'side_effect'; signal='cmd:echo hi > "'+str(marker)+'" exit=0'
        with patch.object(check_completion.subprocess,'run') as run:
            self.assertFalse(check_completion._check_signal(signal)[0]); evidence,_=check_completion.evaluate_signals([signal]); self.assertFalse(evidence[0]['passed']); run.assert_not_called()
        with patch.object(ao_supervisor.subprocess,'run') as run:
            self.assertTrue(ao_supervisor.failed_signals({'signals':[signal]})); run.assert_not_called()
        self.assertFalse(marker.exists())
if __name__=='__main__': unittest.main()
