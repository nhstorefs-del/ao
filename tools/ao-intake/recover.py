"""Read actual AO job states; mutations require --apply, never rerun terminal requests."""
import argparse
import json
import subprocess
import uuid
import os
import sys
from pathlib import Path
from store import Store, DB
from dispatcher import Dispatcher, ROOT
from processes import alive, identity

def jobs_for(rid, jobs_dir):
    jobs = []
    for folder in Path(jobs_dir).glob(rid+'*'):
        if not folder.is_dir():
            continue
        path = folder/'status.json'
        try:
            status = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(status,dict):
                raise ValueError('invalid status')
            jobs.append(dict(status,job_id=path.parent.name))
        except (OSError,ValueError):
            jobs.append({'job_id':path.parent.name,'state':'UNKNOWN'})
    return jobs

def scan(store=None, jobs_dir=None, apply=False, dispatcher=None, is_alive=alive, resume_runner=None):
    store = store or Store(); dispatcher = dispatcher or Dispatcher(store)
    jobs_dir = jobs_dir or ROOT/'tools'/'ao-memory'/'jobs'
    results = []
    for r in store.list():
        if r['state'] not in ('RUNNING','CLAIMED'):
            continue
        cv = store.conversation(r['conversation_id'])
        if is_alive(r['run_pid'],r['run_created']) or is_alive(cv['lock_pid'],cv['lock_created']):
            continue
        jobs = jobs_for(r['request_id'],jobs_dir)
        if jobs:
            for job in jobs:
                state = str(job.get('state','UNKNOWN')).upper()
                item = {'request_id':r['request_id'],'job_id':job['job_id'],'state':state,'action':'NO_TOUCH'}
                if state in ('ORPHANED','STALLED','RUNNING','CLAIMED','RETRYING','MẤT_GIÁM_SÁT') and not is_alive(job.get('supervisor_pid'),job.get('supervisor_created')) and not is_alive(job.get('worker_pid'),job.get('worker_created')):
                    cmd = ['python','tools/ao-memory/ao_run.py','resume','--id',job['job_id']]
                    item.update(action='PROPOSE_RESUME',command=subprocess.list2cmdline(cmd),apply_eligible=state in ('ORPHANED','STALLED'))
                    if apply and item['apply_eligible']:
                        result = resume_runner(cmd) if resume_runner else subprocess.run(cmd,cwd=ROOT,stdin=subprocess.DEVNULL,capture_output=True)
                        item.update(action='RESUME_ATTEMPTED',exit_code=result.returncode)
                results.append(item)
            continue  # existing jobs always block Claude relaunch, even unknown/failed
        if r['attempts'] >= 2:
            results.append({'request_id':r['request_id'],'action':'RETRY_LIMIT'})
            continue
        item = {'request_id':r['request_id'],'action':'PROPOSE_CLAUDE_RECOVERY','attempts':r['attempts'],'snapshot':jobs}
        if apply:
            token = uuid.uuid4().hex
            with store.transaction() as c:
                current = c.execute('SELECT * FROM requests WHERE request_id=?',(r['request_id'],)).fetchone()
                lock = c.execute('SELECT * FROM conversations WHERE conversation_id=?',(r['conversation_id'],)).fetchone()
                if current['state'] not in ('RUNNING','CLAIMED') or current['attempts']>=2 or is_alive(current['run_pid'],current['run_created']) or is_alive(lock['lock_pid'],lock['lock_created']) or jobs_for(r['request_id'],jobs_dir):
                    continue
                c.execute('UPDATE requests SET attempts=attempts+1,claim_by=?,run_pid=?,run_created=? WHERE request_id=?',(token,os.getpid(),identity(os.getpid()),r['request_id']))
                c.execute('UPDATE conversations SET lock_token=?,lock_pid=?,lock_created=?,state=\'BUSY\' WHERE conversation_id=?',(token,os.getpid(),identity(os.getpid()),r['conversation_id']))
                store.event(c,r['request_id'],current['state'],current['state'],'recovery retry reserved; existing jobs=[]')
            item.update(action='CLAUDE_RECOVERY_ATTEMPTED',outcome=dispatcher.execute(r['request_id'],token,{'jobs':jobs,'instruction':'KHÔNG giao lại job đã có'}))
        results.append(item)
    return results

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',type=Path,default=DB)
    sub=p.add_subparsers(dest='action',required=True); s=sub.add_parser('scan'); s.add_argument('--apply',action='store_true')
    a=p.parse_args(); print(json.dumps(scan(Store(a.db),apply=a.apply),ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
