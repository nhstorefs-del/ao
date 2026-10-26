"""Append-only task conversations."""
import json
import re
from pathlib import Path
from common import HERE, locked, now

ROOT = HERE / 'threads'
VALID_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')

def path_for(task_id, root=None):
    if not VALID_ID.fullmatch(task_id):
        raise ValueError('Invalid task id')
    return Path(root or ROOT) / (task_id + '.jsonl')

def read(task_id, root=None, offset=0, limit=100):
    rows = all_messages(task_id, root)
    return rows[max(0, int(offset)):max(0, int(offset)) + min(500, max(1, int(limit)))]

def all_messages(task_id, root=None):
    path = path_for(task_id, root)
    if not path.exists(): return []
    with locked(path):
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    rows=[]; by_id={}
    for record in records:
        if record.get('_update'):
            if record['id'] in by_id: by_id[record['id']].update(record['changes'])
        else:
            by_id[record['id']]=record; rows.append(record)
    return rows

def append(task_id, sender, text, attachments=None, root=None, **extra):
    path = path_for(task_id, root); path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []
        last_id = max((r['id'] for r in records if not r.get('_update')),default=0)
        row = dict(id=last_id + 1, ts=now(), **{'from':sender}, text=text,
                   attachments=attachments or [], status='new' if sender == 'owner' else 'answered')
        row.update(extra)
        with path.open('a', encoding='utf-8') as f: f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row

def update(task_id, message_id, root=None, **changes):
    path = path_for(task_id, root)
    with locked(path):
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        row = next((r for r in records if r['id'] == message_id and not r.get('_update')), None)
        if row is None: raise KeyError(message_id)
        for record in records:
            if record.get('_update') and record['id']==message_id: row.update(record['changes'])
        row.update(changes)
        with path.open('a',encoding='utf-8') as f:
            f.write(json.dumps({'_update':True,'id':message_id,'ts':now(),'changes':changes},ensure_ascii=False)+'\n')
    return row

def pending(root=None):
    result = []
    root = root or ROOT
    for path in Path(root).glob('*.jsonl'):
        for row in all_messages(path.stem, root):
            if row.get('from') == 'owner' and (row.get('status') == 'new' or row.get('need_claude')):
                result.append(dict(task=path.stem, id=row['id'], ts=row['ts'], text=row['text'][:120], status=row['status'], need_claude=bool(row.get('need_claude'))))
    return result
