"""Local append-only approval ledger. Every read verifies the complete hash chain."""
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent / 'data' / 'approvals.jsonl'
REQUEST = re.compile(r'^REQ-[0-9a-fA-F]+$')

def canonical(request_id):
    return 'REQ-' + request_id[4:].lower()


def utc_now():
    return datetime.now(timezone.utc)


def digest(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


@contextmanager
def locked(path):
    """A separate stable lock file serializes full verify + append across processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.seek(0); lock.write(b'0'); lock.flush()
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Ledger:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('AO_APPROVAL_LEDGER') or DEFAULT)

    def _read(self):
        approvals, previous = {}, '0' * 64
        if not self.path.exists():
            return approvals, previous
        with self.path.open('rb') as stream:
            for raw in stream:
                if not raw.endswith(b'\n'):
                    raise ValueError('approval ledger truncated')
                row = json.loads(raw.decode('utf-8'))
                claimed = row.pop('hash')
                if row.get('prev_hash') != previous or digest(row) != claimed:
                    raise ValueError('approval ledger hash mismatch')
                previous = claimed
                if row['op'] == 'record':
                    item = row['approval']
                    if item['id'] in approvals:
                        raise ValueError('duplicate approval')
                    approvals[item['id']] = item
                elif row['op'] == 'consume':
                    item = approvals[row['id']]
                    if item['uses_left'] <= 0:
                        raise ValueError('approval overconsumed')
                    item['uses_left'] -= 1
                    item['uses'].append(row['use'])
                else:
                    raise ValueError('unknown approval operation')
        return approvals, previous

    def _append(self, row, previous):
        row = dict(row, prev_hash=previous)
        row['hash'] = digest(row)
        with self.path.open('ab') as stream:
            stream.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
            stream.flush(); os.fsync(stream.fileno())

    def record(self, session_id, text, request_ids, *, minutes=30, uses=5):
        if not isinstance(text, str) or not text or len(text) > 1000:
            raise ValueError('approval text must be 1..1000 characters')
        if not isinstance(session_id, str) or len(session_id) > 200:
            raise ValueError('invalid session')
        ids = list(dict.fromkeys(canonical(x) for x in request_ids))
        if any(not REQUEST.fullmatch(x) for x in ids):
            raise ValueError('invalid request ID')
        if not 0 < uses <= 5 or not 0 < minutes <= 30:
            raise ValueError('approval limits exceeded')
        at = utc_now()
        item = dict(id='APR-' + uuid.uuid4().hex, at=at.isoformat(), session_id=session_id,
                    text=text, request_ids=ids, expires_at=(at + timedelta(minutes=minutes)).isoformat(),
                    uses_left=uses, uses=[])
        with locked(self.path):
            _, previous = self._read()
            self._append({'op': 'record', 'approval': item}, previous)
        return item

    def find_valid(self, request_id, tool=''):
        if not REQUEST.fullmatch(request_id or ''):
            return None
        with locked(self.path):
            approvals, _ = self._read()
            current = utc_now()
            for item in reversed(list(approvals.values())):
                if (canonical(request_id) in item['request_ids'] and item['uses_left'] > 0
                        and datetime.fromisoformat(item['expires_at']) > current):
                    return item.copy()
        return None

    def get(self, approval_id):
        with locked(self.path):
            approvals, _ = self._read()
            item = approvals.get(approval_id)
            return item.copy() if item else None

    def consume(self, approval_id, tool, summary, *, request_id=None):
        if not isinstance(tool, str) or not tool or len(tool) > 100 or not isinstance(summary, str):
            raise ValueError('invalid use')
        with locked(self.path):
            approvals, previous = self._read()
            item = approvals.get(approval_id)
            if (not item or item['uses_left'] <= 0 or
                datetime.fromisoformat(item['expires_at']) <= utc_now() or
                (request_id is not None and canonical(request_id) not in item['request_ids'])):
                raise ValueError('approval unavailable')
            use = {'at': utc_now().isoformat(), 'tool': tool, 'summary': summary[:80]}
            self._append({'op': 'consume', 'id': approval_id, 'use': use}, previous)
            return dict(item, uses_left=item['uses_left'] - 1, uses=[*item['uses'], use])


def record(session_id, text, request_ids, **kwargs):
    return Ledger().record(session_id, text, request_ids, **kwargs)

def find_valid(request_id, tool=''):
    return Ledger().find_valid(request_id, tool)

def consume(approval_id, tool, summary, **kwargs):
    return Ledger().consume(approval_id, tool, summary, **kwargs)
