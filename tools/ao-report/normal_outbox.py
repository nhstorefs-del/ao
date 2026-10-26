"""Durable ten-minute batching for ordinary completed-job notices."""
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import reporting

OUTBOX = reporting.HERE/'outbox'/'normal.jsonl'
NOISE = reporting.HERE/'noise.jsonl'
WINDOW = timedelta(minutes=10)


def latest(path=OUTBOX):
    items={}
    for row in reporting.rows(path):
        items[(row['id'],row['attempt'])]=row
    return items


def enqueue(path, clock=reporting.now, outbox=OUTBOX, launch=True):
    path=Path(path)
    job=reporting.read(path/'job.json')
    state=reporting.read(path/'status.json')
    attempt=state.get('attempt_id')
    if not attempt or state.get('state')!='DONE_NEEDS_REVIEW': return 'ineligible'
    key=(path.name,attempt)
    with reporting.locked(Path(outbox).with_suffix('.lock')):
        if key in latest(outbox): return 'already_queued'
        attempts=int(state.get('attempts') or 1)
        healed='' if attempts<=1 else f' · đã tự thử lại {attempts-1} lần'
        item={'id':path.name,'attempt':attempt,'path':str(path),
              'title':reporting.clean(job.get('title',path.name),90),
              'artifact':reporting.clean(reporting.result_file(job) or 'chưa có tệp kết quả',180),
              'summary':reporting.summary(reporting.result_file(job))+healed,
              'finished_at':state.get('finished_at') or clock().isoformat(),
              'status':'pending','created_at':clock().isoformat()}
        reporting.append(outbox,item,durable=True)
    if launch:
        # A small Python process sleeps until the batch is due. The watchdog
        # also flushes outbox rows if this process fails or the machine restarts.
        command=[sys.executable,str(Path(__file__).resolve()),'--wake']
        options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,
                 'stderr':subprocess.DEVNULL,'cwd':reporting.HERE,'close_fds':True}
        if os.name=='nt':
            options['creationflags']=subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS
        else: options['start_new_session']=True
        try: subprocess.Popen(command,**options)
        except OSError: pass
    return 'queued'


def _body(batch, clock):
    lines=['[VIỆC XONG — chờ kiểm]']
    for item in batch:
        lines.append(f"{item['title']} ({item['id']}): {item['summary'][:120]} · {item['artifact']} · Chưa kiểm độc lập")
    lines.append(f"Giờ: {clock().astimezone(reporting.BASELINE.tzinfo):%d/%m/%Y %H:%M}")
    return '\n'.join(lines)


def flush(sender=reporting.telegram, clock=reporting.now,
          outbox=OUTBOX, sent=reporting.SENT, force=False):
    if not reporting.enabled(): return 0
    delivered=0
    messages=0
    capped=False
    with reporting.locked(Path(outbox).with_suffix('.lock')):
        rows=[r for r in latest(outbox).values() if r['status']=='pending' and
              int(r.get('tries',0))<3 and
              (not r.get('next_retry') or reporting.parse(r['next_retry'])<=clock())]
        rows.sort(key=lambda r:r['finished_at'])
        while rows:
            first=reporting.parse(rows[0]['finished_at'])
            if not force and first+WINDOW>clock(): break
            batch=[]
            for item in rows:
                when=reporting.parse(item['finished_at'])
                if when-first>WINDOW or len(batch)>=6: break
                candidate=batch+[item]
                if len(_body(candidate,clock))>4096: break
                batch=candidate
            if not batch: batch=rows[:1]
            history=[r for r in reporting.rows(sent) if r.get('ok') and r.get('batch_sha256')]
            hour={r['batch_sha256'] for r in history if reporting.parse(r.get('at')) and
                  clock()-reporting.parse(r['at'])<timedelta(hours=1)}
            day={r['batch_sha256'] for r in history if reporting.parse(r.get('at')) and
                 clock().astimezone(reporting.BASELINE.tzinfo).date()==reporting.parse(r['at']).astimezone(reporting.BASELINE.tzinfo).date()}
            if len(hour)>=6 or len(day)>=20:
                capped=True
                break
            body=_body(batch,clock)
            try: code=sender(body)
            except Exception: code=99
            if code!=0:
                for item in batch:
                    tries=int(item.get('tries',0))+1
                    update={**item,'tries':tries,'result_code':code,'status':'pending',
                            'next_retry':(clock()+timedelta(minutes=5)).isoformat()}
                    reporting.append(outbox,update,durable=True)
                    reporting.append(sent,{'id':item['id'],'attempt':item['attempt'],
                                           'at':clock().isoformat(),'finished_at':item['finished_at'],
                                           'ok':False,'status':'pending','tries':tries,
                                           'result_code':code,
                                           'sha256':hashlib.sha256(body.encode()).hexdigest()},durable=True)
                break
            digest=hashlib.sha256(body.encode('utf-8')).hexdigest()
            messages+=1
            for item in batch:
                update={**item,'status':'delivered','at':clock().isoformat(),'result_code':code,
                        'batch_sha256':digest}
                reporting.append(outbox,update,durable=True)
                reporting.append(sent,{'id':item['id'],'attempt':item['attempt'],
                                       'at':clock().isoformat(),'finished_at':item['finished_at'],
                                       'ok':True,'status':'sent','result_code':code,
                                       'sha256':digest,'batch_sha256':digest},durable=True)
                reporting.mark(Path(item['path']),item['attempt'],True)
                delivered+=1
            consumed={(r['id'],r['attempt']) for r in batch}
            rows=[r for r in rows if (r['id'],r['attempt']) not in consumed]
    if delivered or messages:
        reporting.append(NOISE, {'at':clock().isoformat(),'items':delivered,
                                 'messages':messages,'capped':capped}, durable=True)
    return delivered


def wake():
    for _ in range(3):
        pending=[r for r in latest().values() if r['status']=='pending' and int(r.get('tries',0))<3]
        if not pending: return
        due=min(max(reporting.parse(r['finished_at'])+WINDOW,
                    reporting.parse(r.get('next_retry')) or reporting.parse(r['finished_at'])) for r in pending)
        time.sleep(max(0,(due-reporting.now()).total_seconds()))
        flush()


if __name__=='__main__':
    if '--wake' in sys.argv: wake()
    else: print(json.dumps({'delivered':flush()},ensure_ascii=False))
