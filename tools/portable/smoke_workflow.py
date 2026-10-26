"""Workflow thật với worker Python giả cục bộ; không khóa, không mạng."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT=Path(os.environ.get('AO_ROOT') or Path(__file__).resolve().parents[2]).expanduser().resolve()

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='ao-offline-') as temp:
        d=Path(temp); journal=d/'journal.jsonl'; queue=d/'queue.json'; jobs=d/'jobs'
        brief=d/'brief.md'; brief.write_text('Worker thử cục bộ, tạo file ok.',encoding='utf-8')
        worker=d/'worker.py'; worker.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text("ok",encoding="utf-8")\nprint("FAKE_DONE")\n',encoding='utf-8')
        workers=d/'workers.json'; outputs={x:d/f'{x}.txt' for x in ('A','B')}
        spec={x:dict(enabled=True,command=[sys.executable,str(worker),str(outputs[x])],cwd=str(d),env={},brief_mode='argument',stall_seconds=10,done_markers=['FAKE_DONE'],error_patterns=[]) for x in outputs}
        workers.write_text(json.dumps(spec),encoding='utf-8')
        env=dict(os.environ,PYTHONUTF8='1',AO_ROOT=str(ROOT))
        def run(script,args):
            result=subprocess.run([sys.executable,str(ROOT/script),*map(str,args)],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',creationflags=0x08000000 if os.name=='nt' else 0)
            if result.returncode: raise RuntimeError(f'{script} exit={result.returncode}: {result.stdout} {result.stderr}')
            return result.stdout
        for x in outputs:
            args=['start','--id',x,'--title',f'Thử {x}','--workflow','offline-portable','--lane','A','--worker',x,'--brief',brief,'--signal',f'file:{outputs[x]}','--max-retries','0']
            if x=='B':args+=['--depends-on','A']
            common=['--queue',queue,'--journal',journal,'--jobs',jobs,'--workers',workers]
            run('tools/ao-memory/ao_run.py',common+args)
            deadline=time.monotonic()+5
            while not (jobs/x/'status.json').is_file():
                if time.monotonic()>deadline: raise RuntimeError('Giám sát chưa tạo status')
                time.sleep(0.05)
            result=json.loads(run('tools/ao-memory/ao_run.py',common+['wait','--id',x,'--timeout','15','--poll','0.1']))
            if result['state']!='DONE_NEEDS_REVIEW' or outputs[x].read_text(encoding='utf-8')!='ok':raise RuntimeError('Kết quả worker giả không đạt')
            run('tools/ao-journal/journal.py',['--journal',journal,'verify-task','--task-id',x])
            run('tools/ao-journal/journal.py',['--journal',journal,'accept','--task-id',x,'--by','offline-check'])
        snapshot=json.loads(run('tools/ao-journal/queue.py',['--queue',queue,'--journal',journal,'state','--json']))
        if not all(t['accepted'] for t in snapshot['tasks']):raise RuntimeError('Workflow chưa accepted')
        run('tools/ao-journal/journal.py',['--journal',journal,'verify'])
        print('SMOKE_WORKFLOW PASS: A -> accepted -> B -> accepted; worker Python giả, không mạng')
    return 0

if __name__=='__main__':raise SystemExit(main())
