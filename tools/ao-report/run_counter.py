"""Durable count shared by every recovery type and by reissued job IDs."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import recovery_policy
import reporting

STATE = Path(__file__).resolve().parent / 'state' / 'run_counter.json'


def identity(job):
    brief = Path(job['brief']).read_bytes()
    signals = '\n'.join(sorted(job.get('signals', []))).encode('utf-8')
    return hashlib.sha256(hashlib.sha256(brief).digest() + b'\0' + signals).hexdigest()


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temp.open('w', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def read(path=STATE):
    path = Path(path)
    if not path.exists(): return {'groups': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data.get('groups'), dict): raise ValueError('run_counter hỏng')
    return data


def lookup(job, path=STATE, clock=None):
    clock = clock or datetime.now(timezone.utc)
    digest = identity(job)
    data = read(path)
    for key, row in data['groups'].items():
        if job['id'] in row.get('ids', []):
            return key, row
    for key, row in data['groups'].items():
        if row.get('brief_signal_hash') != digest: continue
        last = reporting.parse(row.get('last_at'))
        if last and clock - last <= timedelta(hours=24): return key, row
    key = 'ID:' + job['id'] + ':' + digest[:12] + ':' + clock.strftime('%Y%m%d%H%M%S')
    return key, data['groups'].get(key, {'runs_total': 0, 'runs': [], 'brief_signal_hash': digest})


def reserve(job, worker, reason, path=STATE, clock=None):
    """Increase and fsync before Popen. Failure means no worker may start."""
    recovery_policy.verify_lock()
    path = Path(path)
    clock = clock or datetime.now(timezone.utc)
    with reporting.locked(path.with_suffix('.lock')):
        data = read(path)
        key, row = lookup(job, path, clock)
        total = int(row['runs_total'])
        allowed = min(recovery_policy.MAX_TOTAL_RUNS, 1 + max(0, int(job.get('max_retries', 2))))
        if total >= allowed:
            raise ValueError(f'Đã chạm trần {total}/{allowed} lần chạy; cần người')
        row['runs_total'] = total + 1
        row['last_at'] = clock.isoformat()
        row.setdefault('ids', [])
        if job['id'] not in row['ids']: row['ids'].append(job['id'])
        row['runs'].append({'attempt_id': job['attempt_id'], 'worker': worker,
                            'type': reason, 'at': clock.isoformat(), 'reason': reason})
        data['groups'][key] = row
        _save(path, data)
        return key, row['runs_total'], allowed
