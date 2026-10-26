"""Loopback-only hub intake. Never starts a dispatcher or writes AO jobs."""
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from store import Store, DB
from approval_ledger import Ledger

BASE = Path(__file__).resolve().parent
MAX_BODY = 20 * 1024

def configure(server, db=None, jobs=None, reports=None):
    server.intake_token = secrets.token_urlsafe(32)
    server.intake_db = Path(db) if db else DB
    server.intake_jobs = Path(jobs) if jobs else BASE.parent / 'ao-memory' / 'jobs'
    server.intake_reports = Path(reports) if reports else BASE / 'requests'
    server.intake_rates = []
    server.intake_lock = threading.Lock()

def guard(h, mutation=False):
    port = h.server.server_port
    allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
    if h.client_address[0] != '127.0.0.1' or h.headers.get('Host') not in allowed:
        return False
    origin = h.headers.get('Origin')
    if (mutation and not origin) or (origin and origin not in {'http://' + x for x in allowed}):
        return False
    token = h.headers.get('X-AO-Token', '')
    if not hmac.compare_digest(token.encode('utf-8'), h.server.intake_token.encode('ascii')):
        return False
    return not h.server.pin or h.authenticated()

def store(h):
    return Store(h.server.intake_db)

def post(h, path):
    h.close_connection = True
    h.connection.settimeout(5)
    def reject(status, message, extra=None):
        # Windows resets a socket closed with an unread ordinary request body.
        # Drain only a bounded body; never allocate/read arbitrary Content-Length.
        try:
            size = int(h.headers.get('Content-Length', '0'))
            if 0 < size <= 65536:
                h.rfile.read(size)
        except (ValueError, OSError):
            pass
        return h.reply(status, {'error': message}, extra)
    if not guard(h, True):
        return reject(403, 'Không được phép ghi yêu cầu')
    try:
        if h.headers.get('Transfer-Encoding') or len(h.headers.get_all('Content-Length', [])) != 1:
            return h.reply(400, {'error': 'Kích thước không hợp lệ'})
        size = int(h.headers.get('Content-Length', '0'))
        if size > MAX_BODY:
            return reject(413, 'Yêu cầu quá lớn (tối đa 20 KB)')
        if size <= 0:
            raise ValueError()
        with h.server.intake_lock:
            t = time.monotonic()
            h.server.intake_rates[:] = [x for x in h.server.intake_rates if t-x < 60]
            if len(h.server.intake_rates) >= 30:
                return reject(429, 'Quá nhiều yêu cầu, hãy thử sau một phút', {'Retry-After': '60'})
            h.server.intake_rates.append(t)
        h.connection.settimeout(5)
        raw = h.rfile.read(size)
        if len(raw) != size:
            raise ValueError()
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        text, key = body.get('text'), body.get('idempotency_key')
        if not isinstance(text, str) or not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError()
        s = store(h)
        if path.endswith('/reply'):
            previous = s.get(body.get('request_id'))
            if not previous:
                return h.reply(404, {'error': 'Không thấy yêu cầu'})
            cv = s.conversation(previous['conversation_id'])
            if not cv['claude_session_id']:
                return h.reply(409, {'error': 'Chưa có phiên để nối tiếp'})
            if previous['state'] != 'NEEDS_OWNER_ANSWER':
                return h.reply(409, {'error': 'Yêu cầu chưa cần Owner trả lời'})
            result = s.submit(text, key, previous['kind'], 'reply', previous['conversation_id'])
            # Reused keys must never acknowledge another conversation's message.
            if result['conversation_id'] != previous['conversation_id'] or result['source'] != 'reply':
                return h.reply(409, {'error': 'Mã gửi đã được dùng cho yêu cầu khác'})
        else:
            kind = body.get('kind', 'task')
            if not isinstance(kind, str):
                raise ValueError()
            result = s.submit(text, key, kind, 'ui')
        return h.reply(200, {'request_id': result['request_id'], 'deduplicated': result['deduplicated'],
                             'similar_to': result['similar_to'], 'state': result['state']})
    except (ValueError, TypeError, UnicodeError):
        return h.reply(400, {'error': 'Nội dung yêu cầu không hợp lệ'})
    except Exception:
        return h.reply(503, {'error': 'Chưa ghi nhận được, hãy gửi lại với cùng mã gửi'})

def jobs(h, rid):
    rows = []
    for p in h.server.intake_jobs.glob(rid + '*/status.json'):
        try:
            if p.is_symlink() or p.parent.is_symlink() or p.stat().st_size > 256*1024:
                continue
            d = json.loads(p.read_text(encoding='utf-8'))
            meta = p.parent / 'job.json'
            metadata = {}
            if meta.is_file() and not meta.is_symlink() and meta.stat().st_size <= 256*1024:
                try:
                    metadata = json.loads(meta.read_text(encoding='utf-8'))
                except (ValueError, OSError):
                    pass
            rows.append({'id': p.parent.name, **{k: d.get(k) for k in
                         ('status', 'state', 'updated_at', 'last_error')},
                         'worker': d.get('worker') or metadata.get('worker'),
                         'lane': d.get('lane') or metadata.get('lane')})
        except (OSError, ValueError, AttributeError):
            rows.append({'id': p.parent.name, 'status': 'UNKNOWN', 'last_error': 'Không đọc được trạng thái thật'})
    return rows

def report(h, r):
    root = h.server.intake_reports.resolve()
    p = Path(r['report_path']) if r.get('report_path') else root / r['request_id'] / 'report.md'
    try:
        p = p.resolve()
        if not p.is_relative_to(root) or p.name != 'report.md' or not p.is_file():
            return None
        if p.stat().st_size > 1024*1024:
            return 'Báo cáo vượt giới hạn hiển thị 1 MB.'
        return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None

def get(h, path):
    if not guard(h):
        return h.reply(403, {'error': 'Không được phép đọc yêu cầu'})
    s = store(h)
    if path == '/api/intake/requests':
        return h.reply(200, {'requests': [dict(r, jobs=jobs(h, r['request_id'])) for r in reversed(s.list())]})
    rid = path.rsplit('/', 1)[-1]
    if not rid.startswith('REQ-') or not rid[4:].isalnum() or len(rid) > 80:
        return h.reply(404, {'error': 'Không thấy yêu cầu'})
    r = s.get(rid)
    if not r:
        return h.reply(404, {'error': 'Không thấy yêu cầu'})
    with s.connect() as c:
        events = [dict(x) for x in c.execute('SELECT * FROM events WHERE request_id=? ORDER BY seq', (rid,))]
    approval = None
    if r.get('approved_by'):
        try:
            item = Ledger().get(r['approved_by'])
            if item and rid in item['request_ids']:
                approval = {key: item[key] for key in ('id', 'at', 'expires_at')}
        except (OSError, ValueError, KeyError):
            pass
    return h.reply(200, {'request': r, 'messages': s.messages(r['conversation_id']),
                         'events': events, 'jobs': jobs(h, rid), 'report': report(h, r),
                         'approval': approval})
