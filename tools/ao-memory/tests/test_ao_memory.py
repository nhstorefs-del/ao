import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import registry, ao_decide, ao_state, ao_recall

class MemoryTests(unittest.TestCase):
    def test_registry_parallel(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'projects.json'
            registry.seed(path,{'tasks':[]})
            threads=[threading.Thread(target=registry.change,args=('example',path),kwargs={'task':f'T{i}'}) for i in range(20)]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual(20,len(next(r for r in registry.all_projects(path) if r['id']=='example')['task_ids']))
    def test_decisions_import_verify(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'decisions.jsonl'; source=Path(d)/'approvals.md'
            source.write_text('- 2026-10-02 · "Chọn mẫu 2. Phù hợp." · giọng lời đọc\n- bad line\n',encoding='utf-8')
            self.assertEqual((1,1),ao_decide.import_approvals(source,path))
            self.assertEqual((0,2),ao_decide.import_approvals(source,path))
            self.assertTrue(ao_decide.verify(path))
            raw=path.read_text(encoding='utf-8').replace('Phù hợp','Không hợp')
            path.write_text(raw,encoding='utf-8'); self.assertFalse(ao_decide.verify(path))
    def test_state_sections_missing(self):
        out=ao_state.generate(ledger={'tasks':[]},projects=[],decisions=[],rules='- Quy tắc',taskboard={})
        for head in ao_state.HEADS:self.assertIn(head,out['markdown'])
        original=ao_state.ledger_state
        try:
            ao_state.ledger_state=lambda: (_ for _ in ()).throw(FileNotFoundError('missing'))
            self.assertIn('không đọc được: sổ việc',ao_state.generate(projects=[],decisions=[],rules='x',taskboard={})['markdown'])
        finally: ao_state.ledger_state=original
    def test_recall_unicode_secret(self):
        with tempfile.TemporaryDirectory() as d:
            m=Path(d); (m/'note.md').write_text('dự án mẫu có sk-SECRET123 và ghi chú phụ',encoding='utf-8')
            out=ao_recall.search('dự án mẫu',sources=['memory'],memory=m,sessions=m)
            self.assertEqual(1,len(out['memory']))
            self.assertNotIn('sk-SECRET123',out['memory'][0]['excerpt'])
            self.assertTrue(ao_recall.search('du an mau',sources=['memory'],memory=m,sessions=m)['memory'])
    def test_state_brief_real(self):
        out=ao_state.generate(brief=True)['markdown']
        self.assertLessEqual(len(out),8000)
        self.assertTrue(out.startswith('AO STATE ('))
        for head in (ao_state.HEADS[i] for i in (0,1,2,3,5,6)):
            self.assertIn('## '+head,out)
        self.assertIn('ao_toolbox.py find',out)
        self.assertNotIn('## DỰ ÁN',out)
        self.assertNotIn('## (8) CÔNG CỤ',out)
        # An accepted task can retain an old job status; the brief must hide it.
        ledger=ao_state.ledger_state()
        accepted={t.get('id') for t in ledger.get('tasks',[]) if t.get('status')=='accepted'}
        for tid in accepted:
            job=ao_state.read_json(ao_state.HERE/'jobs'/str(tid)/'status.json',{})
            if job.get('state')=='DONE_NEEDS_REVIEW':
                self.assertNotIn(f'{tid}: DONE_NEEDS_REVIEW',out)
    def test_state_brief_missing_and_acceptance(self):
        fake={'tasks':[{'id':'OLD','status':'accepted','title':'accepted'},
                       {'id':'NEW','status':'done','title':'review','ts':'2020-01-01T00:00:00Z'}]}
        original=ao_state.ledger_state
        try:
            ao_state.ledger_state=lambda: (_ for _ in ()).throw(FileNotFoundError('missing'))
            missing=ao_state.generate(projects=[],decisions=[],rules='',taskboard={},brief=True)['markdown']
            self.assertIn('không đọc được: sổ việc',missing)
        finally: ao_state.ledger_state=original
        with patch.object(ao_state,'read_json',return_value={'state':'DONE_NEEDS_REVIEW','retries':0}):
            out=ao_state.generate(ledger=fake,projects=[],decisions=[],rules='- Quy tắc',taskboard={},brief=True)['markdown']
        self.assertNotIn('OLD: DONE_NEEDS_REVIEW',out)
        self.assertIn('NEW: DONE_NEEDS_REVIEW',out)
        self.assertIn('Done chưa accepted: NEW',out)
if __name__=='__main__':unittest.main()
