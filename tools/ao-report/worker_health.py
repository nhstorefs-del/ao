"""Durable circuit breaker for repeated worker/signature failures."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import reporting
import run_counter

STATE = reporting.HERE/'state'/'worker_health.json'
WINDOW = timedelta(minutes=30)
REST = timedelta(minutes=60)


def read(path=STATE):
    path=Path(path)
    if not path.exists(): return {'workers':{}}
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data.get('workers'),dict): raise ValueError('worker_health hỏng')
    return data


def record(worker, signature, clock=None, path=STATE, key=None):
    clock=clock or datetime.now(timezone.utc)
    path=Path(path)
    with reporting.locked(path.with_suffix('.lock')):
        data=read(path)
        row=data['workers'].setdefault(worker,{'failures':[]})
        row['failures']=[r for r in row.get('failures',[]) if
                         (reporting.parse(r.get('at')) or clock-WINDOW*2)>=clock-WINDOW]
        row['failures'].append({'at':clock.isoformat(),'signature':signature,
                                'key': key})
        relevant=[r for r in row['failures'] if r['signature']==signature]
        if all(r.get('key') is None for r in relevant):
            same=len(relevant)
        else:
            same=len({r.get('key') for r in relevant if r.get('key') is not None})
        opened=same>=3 and not is_open(worker,clock,data)
        if opened: row['open_until']=(clock+REST).isoformat()
        run_counter._save(path,data)
        return {'opened':opened,'same_signature_count':same,
                'open_until':row.get('open_until')}


def is_open(worker, clock=None, data=None, path=STATE):
    clock=clock or datetime.now(timezone.utc)
    data=data or read(path)
    until=reporting.parse(data.get('workers',{}).get(worker,{}).get('open_until'))
    return bool(until and until>clock)
