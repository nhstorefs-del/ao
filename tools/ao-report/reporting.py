"""Independent, best-effort Owner notifications for AO job attempts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOBS = HERE.parent / 'ao-memory' / 'jobs'
SENT = HERE / 'sent.jsonl'
BASELINE = datetime.fromisoformat('2026-10-11T00:00:00+07:00')
STATES = {'DONE_NEEDS_REVIEW', 'FAILED', 'BLOCKED', 'STOPPED', 'STALLED', 'ORPHAN', 'ORPHANED', 'MAX_RUNTIME', 'GAVE_UP', 'SUPERVISOR_ERROR'}
JOB_LOG = HERE / 'JOBS.log'
JOB_LOG_PENDING = HERE / 'JOBS.pending.jsonl'
RECOVERY = HERE / 'recovery.jsonl'
UNKNOWN = HERE / 'error_patterns_unknown.jsonl'
TOKEN = re.compile(r'(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}')
PRIVATE = re.compile(r'(?i)(?:\.worker-keys|telegram\.env|key[_-]?file|token|secret|api[_-]?key)')


def enabled():
    return os.environ.get('AO_AUTOREPORT', '1').strip().lower() not in {'0', 'false', 'off'}


def now():
    return datetime.now(timezone.utc)


def parse(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def rows(path=SENT):
    try:
        return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def append(path, row, durable=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('a', encoding='utf-8') as file:
        file.write(json.dumps(row, ensure_ascii=False) + '\n')
        file.flush()
        if durable:
            os.fsync(file.fileno())


def job_event(job, worker, kind, note='', log=JOB_LOG, pending=JOB_LOG_PENDING):
    """Durable, readable lifecycle entry. A failed append is queued for repair."""
    ident = clean(job.get('id'), 80)
    attempt = clean(job.get('attempt_id'), 80)
    line = f'{now().isoformat()} {ident} {attempt} {clean(worker, 80)} {kind} {clean(note, 160)}'.rstrip() + '\n'
    try:
        with Path(log).open('a', encoding='utf-8') as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
        return True
    except OSError:
        try:
            with Path(pending).open('a', encoding='utf-8') as file:
                file.write(json.dumps({'line': line}, ensure_ascii=False) + '\n')
                file.flush()
                os.fsync(file.fileno())
        except OSError:
            pass
        return False


def repair_job_log(log=JOB_LOG, pending=JOB_LOG_PENDING):
    pending = Path(pending)
    if not pending.exists():
        return 0
    try:
        lines = [json.loads(x)['line'] for x in pending.read_text(encoding='utf-8').splitlines() if x]
        with Path(log).open('a', encoding='utf-8') as file:
            file.writelines(lines)
            file.flush()
            os.fsync(file.fileno())
        pending.unlink()
        return len(lines)
    except (OSError, ValueError, KeyError):
        return 0


def recovery(job, classification, action, target, reason, policy_detail=None):
    """Keep automated decisions separate from worker status and never leak raw logs."""
    row = {'at': now().isoformat(), 'id': job['id'], 'attempt': job['attempt_id'],
           'worker': job.get('current_worker', job['worker']), 'type': classification['type'],
           'signature': classification['signature'], 'action': action, 'target': target,
           'reason': clean(reason, 160)}
    if isinstance(policy_detail, dict):
        row.update(policy_detail)
    append(RECOVERY, row)
    if classification['type'] == 'UNKNOWN':
        append(UNKNOWN, {k: row[k] for k in ('at', 'signature', 'worker', 'reason')})


@contextmanager
def locked(path):
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        file.seek(0)
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(.1)
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def clean(value, limit=180):
    value = TOKEN.sub('[ẩn]', str(value or ''))
    value = re.sub(r'[\r\n\t]+', ' ', value).strip()
    if PRIVATE.search(value):
        return '[ẩn nội dung nhạy cảm]'
    return value[:limit]


def attempt_start(path, attempt):
    for line in (path / 'events.jsonl').read_text(encoding='utf-8').splitlines() if (path / 'events.jsonl').exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'START' and attempt in event.get('detail', ''):
            return parse(event.get('ts'))
    # Attempt #1 can predate the first START event (old jobs remain legacy).
    if str(attempt).endswith('#1'):
        return parse(read(path / 'status.json').get('started_at')) or parse(read(path / 'job.json').get('created_at'))
    return None


def closed_jobs():
    """Việc Owner đã đóng nhãn (closed_jobs.json). Lỗi đọc ⇒ rỗng (fail-open)."""
    try:
        return set(json.loads((HERE / 'closed_jobs.json').read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        return set()


def legacy(path, attempt):
    started = attempt_start(path, attempt)
    return started is None or started < BASELINE


def result_file(job):
    for signal in job.get('signals', []):
        if signal.startswith('file:'):
            candidate = Path(signal[5:].strip().removesuffix(' exists').strip())
            if candidate.is_file():
                return candidate
    return None


def summary(path):
    if not path:
        return 'chưa có tóm tắt'
    try:
        if path.suffix.lower() == '.json':
            data = read(path)
            if not isinstance(data, dict):
                return 'chưa có tóm tắt'
            parts = [clean(data.get(key)) for key in ('status', 'notes_for_owner', 'summary') if data.get(key)]
            return ' | '.join(parts)[:300] or 'chưa có tóm tắt'
        text = path.read_text(encoding='utf-8-sig', errors='replace')[:3000]
        found = []
        for line in text.splitlines():
            match = re.match(r'^\s*(?:status|notes_for_owner|summary)\s*[:=]\s*(.+)', line, re.I)
            if match:
                found.append(clean(match.group(1)))
        return ' | '.join(found)[:300] or 'chưa có tóm tắt'
    except OSError:
        return 'chưa có tóm tắt'


def message(path, state, finished):
    job = read(path / 'job.json')
    artifact = result_file(job)
    artifact_text = clean(artifact, 250) if artifact else 'chưa có tệp kết quả'
    blocked = state in {'BLOCKED', 'STALLED', 'ORPHAN', 'ORPHANED', 'MAX_RUNTIME'}
    prefix = '[VIỆC BỊ CHẶN]' if blocked else '[VIỆC XONG — chờ kiểm]'
    reason = clean(read(path / 'status.json').get('reason'), 160)
    if os.environ.get('AO_REPORT_SELFTEST') == '1': prefix = '[THỬ NGHIỆM] ' + prefix
    lines = [f'{prefix} {clean(job.get("title", path.name))} ({clean(job.get("id", path.name))})',
             f'Trạng thái: {clean(state)}', f'Tóm tắt: {summary(artifact)}',
             f'Kết quả: {artifact_text}', 'Chưa kiểm độc lập',
             f'Giờ: {finished.astimezone(BASELINE.tzinfo).strftime("%d/%m/%Y %H:%M %z")}']
    if blocked:
        lines.insert(2, f'Lý do: {reason or "worker bị chặn; cần xử lý"}')
    return '\n'.join(lines)


def telegram(text):
    outbox = os.environ.get('AO_REPORT_TEST_OUTBOX')
    if outbox and os.environ.get('AO_REPORT_SELFTEST') == '1':
        append(outbox, {'at': now().isoformat(), 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'text': text})
        return 0
    from telegram_mute import record_if_muted
    if record_if_muted(text, HERE):
        return 0
    command = [sys.executable, str(HERE.parent / 'telegram-send' / 'telegram_send.py'), '--text', text]
    try:
        proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=75, check=False)
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        return 99


def latest(path, attempt, sent=SENT):
    return next((r for r in reversed(rows(sent)) if r.get('id') == path.name and r.get('attempt') == attempt), None)


def mark(path, attempt, notified):
    status_file = path / 'status.json'
    state = read(status_file)
    if state.get('attempt_id') == attempt and state.get('owner_notified') != notified:
        state['owner_notified'] = notified
        tmp = status_file.with_suffix('.report.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, status_file)


def notify(path, state=None, finished=None, sender=telegram, sent=SENT, clock=now):
    """One successful send per attempt; three total attempts, five minutes apart."""
    if not enabled():
        return 'disabled'
    path = Path(path)
    # Isolated test job roots must opt in explicitly. This prevents an ordinary
    # unit test from sending a real Telegram when its fake worker fails.
    if path.parent.resolve() != JOBS.resolve() and os.environ.get('AO_REPORT_SELFTEST') != '1' and sender is telegram:
        return 'isolated_test_root'
    job, status = read(path / 'job.json'), read(path / 'status.json')
    attempt = status.get('attempt_id') or job.get('attempt_id')
    state = state or status.get('state')
    if not attempt or state not in STATES:
        return 'ineligible'
    if legacy(path, attempt):
        return 'legacy'
    if state == 'DONE_NEEDS_REVIEW' and sender is telegram and Path(sent) == SENT:
        import normal_outbox
        return normal_outbox.enqueue(path, clock=clock)
    # Human decisions have a separate durable lane, without the ordinary cap.
    if state != 'DONE_NEEDS_REVIEW' and status.get('error_type') in {'NEEDS_HUMAN', 'ESCALATE'}:
        import human_outbox
        count = human_outbox.enqueue(path, status, sender=sender, clock=clock)
        mark(path, attempt, count > 0 or any(x['id'] == path.name and x['attempt'] == attempt and x['status'] == 'delivered' for x in human_outbox.state().values()))
        return 'human_sent' if count else 'human_pending'
    finished = finished or parse(status.get('finished_at')) or clock()
    with locked(Path(sent).with_suffix('.lock')):
        previous = latest(path, attempt, sent)
        if previous and previous.get('ok'):
            mark(path, attempt, True)
            return 'already_sent'
        if previous and previous.get('status') in {'deduped', 'throttled'}:
            return previous['status']
        count = previous.get('tries', 0) if previous else 0
        if count >= 3:
            return 'exhausted'
        if previous and (parse(previous.get('next_retry')) or clock()) > clock():
            return 'pending'
        body = message(path, state, finished)
        # Signature dedupe and a strict hourly/daily cap apply to error alerts.
        if state not in {'DONE_NEEDS_REVIEW'}:
            signature = status.get('error_signature')
            history = rows(sent)
            recent = [r for r in history if r.get('ok') and parse(r.get('at'))]
            six_hours = [r for r in recent if clock() - parse(r['at']) < timedelta(hours=6)]
            if signature and any(r.get('signature') == signature for r in six_hours):
                append(sent, {'id': path.name, 'attempt': attempt, 'at': clock().isoformat(),
                              'ok': False, 'status': 'deduped', 'signature': signature})
                return 'deduped'
            hour = sum(clock() - parse(r['at']) < timedelta(hours=1) for r in recent)
            day = sum(clock().astimezone(BASELINE.tzinfo).date() == parse(r['at']).astimezone(BASELINE.tzinfo).date() for r in recent)
            if hour >= 6 or day >= 20:
                append(sent, {'id': path.name, 'attempt': attempt, 'at': clock().isoformat(),
                              'ok': False, 'status': 'throttled', 'signature': signature})
                return 'throttled'
        try:
            code = sender(body)
        except Exception:
            code = 99
        ok = code == 0
        row = {'id': path.name, 'attempt': attempt, 'at': clock().isoformat(),
               'finished_at': finished.isoformat(), 'result_code': code, 'ok': ok,
               'status': 'sent' if ok else 'pending', 'tries': count + 1,
               'next_retry': None if ok else (clock() + timedelta(minutes=5)).isoformat(),
               'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
               'signature': status.get('error_signature')}
        append(sent, row)
        mark(path, attempt, ok)
        return row['status']


def accepted_ids(journal):
    try:
        return {r['task_id'] for r in rows(journal) if r.get('status') == 'accepted'}
    except KeyError:
        return set()


def pending(jobs=JOBS, sent=SENT, clock=now):
    out = []
    accepted_cache = {}
    for path in Path(jobs).glob('*'):
        status, job = read(path / 'status.json'), read(path / 'job.json')
        attempt = status.get('attempt_id')
        if status.get('state') not in STATES or not attempt or legacy(path, attempt) or path.name in closed_jobs():
            continue
        journal = str(job.get('journal') or HERE.parent / 'ao-journal' / 'journal.jsonl')
        if journal not in accepted_cache:
            accepted_cache[journal] = accepted_ids(journal)
        if path.name in accepted_cache[journal]:
            continue
        previous = latest(path, attempt, sent)
        finished = parse(status.get('finished_at')) or (parse(previous.get('finished_at')) if previous else None)
        if not finished:
            continue
        age = max(0, (clock() - finished).total_seconds() / 60)
        notified = bool(previous and previous.get('ok'))
        if not notified or age > 60:
            out.append({'id': path.name, 'attempt': attempt, 'title': clean(job.get('title', path.name)),
                        'state': status['state'], 'age_minutes': round(age, 1), 'owner_notified': notified,
                        'path': path})
    return out
