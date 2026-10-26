"""Keep regression evidence within intake; never overwrite reporting artifacts."""
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]
EVIDENCE=BASE/'evidence'

def hashes():
    rows={}
    # Sources/integrity anchors affected by regression; deliberately exclude live AO ledgers.
    for folder in [ROOT/'tools'/'ao-report',ROOT/'tools'/'ao-memory',ROOT/'tools'/'ao-intake'/'step1_test',ROOT/'projects'/'AO_QA_20261011'/'independent_tests']:
        for path in folder.rglob('*'):
            immutable_docs = folder.name in ('step1_test','independent_tests')
            if path.is_file() and '__pycache__' not in path.parts and (path.suffix=='.py' or immutable_docs and (path.suffix=='.md' or path.name=='scenarios.json')):
                rows[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return rows

def main():
    EVIDENCE.mkdir(exist_ok=True)
    before=hashes()
    (EVIDENCE/'protected_before.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
    commands=[('intake',[sys.executable,'-m','unittest','test_intake'],BASE),
              ('reporting',[sys.executable,'-m','unittest','test_reporting','test_owner_points','test_counter_v2','test_normal_outbox','test_worker_health','test_lane_status','test_telegram_mute'],ROOT/'tools'/'ao-report')]
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8')
    results=[]
    for label,cmd,cwd in commands:
        p=subprocess.run(cmd,cwd=cwd,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',timeout=180)
        (EVIDENCE/(label+'.txt')).write_text(p.stdout+p.stderr,encoding='utf-8')
        results.append({'check':label,'command':subprocess.list2cmdline(cmd),'exit_code':p.returncode})
        print(label,p.returncode,(p.stdout+p.stderr)[-300:])
    # Original main/evaluator, only OUTPUT redirected to respect the brief's write scope.
    cmd=[sys.executable,'-B','-c',"import sys; from pathlib import Path; sys.path.insert(0, 'tools/ao-report'); import run_independent as r; r.OUTPUT=Path('tools/ao-intake/evidence/independent_results.json'); raise SystemExit(r.main())"]
    p=subprocess.run(cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',timeout=180)
    (EVIDENCE/'independent.txt').write_text(p.stdout+p.stderr,encoding='utf-8')
    results.append({'check':'independent','command':subprocess.list2cmdline(cmd),'exit_code':p.returncode})
    print('independent',p.returncode,(p.stdout+p.stderr)[-250:])
    after=hashes(); changed=[k for k in set(before)|set(after) if before.get(k)!=after.get(k)]
    results.append({'check':'protected source hashes','changed':changed,'exit_code':int(bool(changed))})
    (EVIDENCE/'verification.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print('protected source changes',changed)
    return int(any(r['exit_code'] for r in results))

if __name__=='__main__':
    raise SystemExit(main())
