"""Durable intake ledger. All acknowledgements follow a FULL synchronous commit."""
import argparse
import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / 'data' / 'intake.db'
STATES = ('RECORDED', 'CLAIMED', 'RUNNING', 'NEEDS_OWNER_ANSWER', 'WAITING_OWNER', 'DONE', 'FAILED', 'WAITING_QUOTA')
EDGES = {'RECORDED': {'CLAIMED'}, 'CLAIMED': {'RUNNING', 'WAITING_OWNER', 'FAILED'},
         'RUNNING': set(STATES[3:]), 'WAITING_OWNER': {'RECORDED'}}

def now():
    return datetime.now(timezone.utc).isoformat()

class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

class Store:
    def __init__(self, path=DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS conversations(
              conversation_id TEXT PRIMARY KEY, claude_session_id TEXT,
              turn_count INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'IDLE',
              lock_token TEXT, lock_pid INTEGER, lock_created TEXT);
            CREATE TABLE IF NOT EXISTS requests(
              request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
              text TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('task','ask')),
              state TEXT NOT NULL CHECK(state IN ('RECORDED','CLAIMED','RUNNING','NEEDS_OWNER_ANSWER','WAITING_OWNER','DONE','FAILED','WAITING_QUOTA')),
              conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
              lane_label TEXT, claim_by TEXT, claim_at TEXT, run_pid INTEGER,
              run_created TEXT, attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, report_path TEXT, similar_to TEXT);
            CREATE TABLE IF NOT EXISTS messages(
              msg_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('owner','claude')), text TEXT NOT NULL,
              at TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES requests(request_id));
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
              at TEXT NOT NULL, state_from TEXT, state_to TEXT, note TEXT);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
              BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
              BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS forward_only BEFORE UPDATE OF state ON requests
              WHEN NEW.state != OLD.state AND NOT (
                (OLD.state='RECORDED' AND NEW.state='CLAIMED') OR
                (OLD.state='CLAIMED' AND NEW.state IN ('RUNNING','WAITING_OWNER','FAILED')) OR
                (OLD.state='RUNNING' AND NEW.state IN ('NEEDS_OWNER_ANSWER','WAITING_OWNER','DONE','FAILED','WAITING_QUOTA')))
              BEGIN SELECT RAISE(ABORT,'invalid state transition'); END;
            CREATE INDEX IF NOT EXISTS requests_state ON requests(state,created_at);
            ''')
        # Additive migration only: retain existing requests, events and triggers.
        with self.transaction() as c:
            columns = {row['name'] for row in c.execute('PRAGMA table_info(requests)')}
            if 'total_cost_usd' not in columns:
                c.execute('ALTER TABLE requests ADD COLUMN total_cost_usd REAL NOT NULL DEFAULT 0')
            if 'approved_by' not in columns:
                c.execute('ALTER TABLE requests ADD COLUMN approved_by TEXT')
            # SQLite cannot alter a trigger in place; replace just this known trigger.
            c.execute('DROP TRIGGER IF EXISTS forward_only')
            c.execute('''CREATE TRIGGER forward_only BEFORE UPDATE OF state ON requests
              WHEN NEW.state != OLD.state AND NOT (
                (OLD.state='RECORDED' AND NEW.state='CLAIMED') OR
                (OLD.state='CLAIMED' AND NEW.state IN ('RUNNING','WAITING_OWNER','FAILED')) OR
                (OLD.state='RUNNING' AND NEW.state IN ('NEEDS_OWNER_ANSWER','WAITING_OWNER','DONE','FAILED','WAITING_QUOTA')) OR
                (OLD.state='WAITING_OWNER' AND NEW.state='RECORDED' AND NEW.approved_by IS NOT NULL))
              BEGIN SELECT RAISE(ABORT,'invalid state transition'); END''')

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=Connection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA foreign_keys=ON')
        return c

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def submit(self, text, idempotency_key, kind='task', source='cli', conversation_id=None, before_commit=None):
        if not text.strip() or not idempotency_key or kind not in ('task', 'ask'):
            raise ValueError('text/key/kind invalid')
        with self.transaction() as c:
            old = c.execute('SELECT * FROM requests WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if old:
                result = dict(old, deduplicated=True)
            else:
                rid = 'REQ-' + uuid.uuid4().hex
                cid = conversation_id or 'CONV-' + uuid.uuid4().hex
                if conversation_id:
                    if not c.execute('SELECT 1 FROM conversations WHERE conversation_id=?', (cid,)).fetchone():
                        raise ValueError('unknown conversation')
                else:
                    c.execute('INSERT INTO conversations(conversation_id) VALUES(?)', (cid,))
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                similar = c.execute('SELECT request_id FROM requests WHERE text=? AND created_at>=? ORDER BY created_at DESC LIMIT 1', (text, cutoff)).fetchone()
                c.execute('INSERT INTO requests(request_id,idempotency_key,text,created_at,source,kind,state,conversation_id,similar_to) VALUES(?,?,?,?,?,?,\'RECORDED\',?,?)',
                          (rid, idempotency_key, text, now(), source, kind, cid, similar[0] if similar else None))
                self.message(c, cid, 'owner', text, rid)
                self.event(c, rid, None, 'RECORDED', 'durably submitted')
                result = dict(c.execute('SELECT * FROM requests WHERE request_id=?', (rid,)).fetchone(), deduplicated=False)
            if before_commit:
                before_commit(c)
        return result  # transaction has committed; never acknowledge earlier

    @staticmethod
    def message(c, cid, role, text, rid):
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)', (uuid.uuid4().hex, cid, role, text, now(), rid))

    @staticmethod
    def event(c, rid, old, new, note):
        c.execute('INSERT INTO events(request_id,at,state_from,state_to,note) VALUES(?,?,?,?,?)', (rid, now(), old, new, note))

    def cas(self, rid, old, new, note='', **fields):
        if new not in EDGES.get(old, set()):
            return False
        allowed = {'claim_by','claim_at','run_pid','run_created','last_error','report_path','lane_label','approved_by'}
        if set(fields) - allowed:
            raise ValueError('invalid fields')
        with self.transaction() as c:
            cols = ','.join(['state=?'] + [f'{k}=?' for k in fields])
            changed = c.execute(f'UPDATE requests SET {cols} WHERE request_id=? AND state=?', (new, *fields.values(), rid, old)).rowcount
            if changed:
                self.event(c, rid, old, new, note)
        return bool(changed)

    def get(self, rid):
        with self.connect() as c:
            row = c.execute('SELECT * FROM requests WHERE request_id=?', (rid,)).fetchone()
            return dict(row) if row else None

    def list(self, state=None):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM requests' + (' WHERE state=?' if state else '') + ' ORDER BY created_at,request_id', (state,) if state else ())]

    def conversation(self, cid):
        with self.connect() as c:
            r = c.execute('SELECT * FROM conversations WHERE conversation_id=?', (cid,)).fetchone()
            return dict(r) if r else None

    def messages(self, cid):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY at,rowid', (cid,))]

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, default=DB)
    sub = p.add_subparsers(dest='action', required=True)
    s = sub.add_parser('submit')
    s.add_argument('--text', required=True); s.add_argument('--key', required=True)
    s.add_argument('--kind', choices=('task','ask'), default='task'); s.add_argument('--source', default='cli')
    sub.add_parser('show').add_argument('--id', required=True)
    sub.add_parser('list').add_argument('--state', choices=STATES)
    a = p.parse_args(); s = Store(a.db)
    result = s.submit(a.text,a.key,a.kind,a.source) if a.action=='submit' else s.get(a.id) if a.action=='show' else s.list(a.state)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
