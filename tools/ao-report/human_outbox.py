"""Durable human escalation lane. Telegram is only a transport, never the ledger."""
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

import reporting

# Test/sandbox isolation: an explicit env override keeps a harness run from
# touching the real escalation ledger (and lets it count real sends on a clean
# slate). Unset => production paths, unchanged.
OUTBOX = Path(os.environ.get('AO_HUMAN_OUTBOX') or (reporting.HERE / 'outbox' / 'human.jsonl'))
NEEDS = Path(os.environ.get('AO_NEEDS_HUMAN') or (reporting.HERE / 'needs_human.md'))
DELAYS = (1, 2, 5, 15)
DEDUPE_WINDOW = timedelta(hours=6)


def state(path=OUTBOX):
    latest = {}
    for row in reporting.rows(path):
        latest[row['escalation_id']] = row
    return latest


def _signature_times(outbox, signature, clock, status='delivered'):
    found = []
    for row in state(outbox).values():
        if row.get('signature') != signature:
            continue
        if status and row.get('status') != status:
            continue
        stamp = reporting.parse(row.get('at') or row.get('created_at'))
        if stamp:
            found.append(stamp)
    return found


def enqueue(job_path, status, sender=reporting.telegram, clock=reporting.now,
            outbox=OUTBOX, needs=NEEDS):
    """Persist before sending. A new job always has its own escalation item."""
    job_path = Path(job_path)
    job = reporting.read(job_path / 'job.json')
    attempt = status.get('attempt_id') or job.get('attempt_id')
    signature = status.get('error_signature') or 'unknown'
    escalation_id = hashlib.sha256(f'{job_path.name}|{attempt}|{signature}'.encode()).hexdigest()[:20]
    title = reporting.clean(job.get('title', job_path.name), 100)
    reason = reporting.clean(status.get('reason') or status.get('error_type') or 'Cần kiểm tra', 160)
    body = (f'[VIỆC CẦN BẠN QUYẾT] {title} ({job_path.name})\nLý do: {reason}\n'
            f'→ cần bạn quyết\nMã lỗi (signature): {signature}\n'
            f'Để học: nói với điều phối viên cách xử lý, nó sẽ ghi bằng learn.py')
    display = reporting.clean(status.get('runs_display'), 40)
    if display:
        body += f'\nTiến trình: {display}'
    if os.environ.get('AO_REPORT_SELFTEST') == '1':
        body = '[THỬ NGHIỆM] ' + body
    deduped = False
    storm = False
    with reporting.locked(Path(outbox).with_suffix('.lock')):
        records = state(outbox)
        if escalation_id not in records:
            delivered_recent = [stamp for stamp in _signature_times(outbox, signature, clock, status='delivered')
                                if clock() - stamp <= DEDUPE_WINDOW]
            all_recent = [stamp for stamp in _signature_times(outbox, signature, clock, status=None)
                          if clock() - stamp <= DEDUPE_WINDOW]
            deduped = bool(delivered_recent) and len(all_recent) <= 20
            storm = len(all_recent) > 20
            item = {'escalation_id': escalation_id, 'id': job_path.name, 'attempt': attempt,
                    'job_path': str(job_path),
                    'signature': signature, 'lane': 'human', 'body': body,
                    'created_at': clock().isoformat(),
                    'status': 'deduped' if deduped else 'pending', 'tries': 0,
                    'next_retry': clock().isoformat()}
            reporting.append(outbox, item, durable=True)
            with Path(needs).open('a', encoding='utf-8') as f:
                f.write(f'- [{escalation_id}] {title} ({job_path.name}): {reason} | signature_id={signature}\n')
                f.flush(); os.fsync(f.fileno())
    if deduped or storm:
        return 0
    return deliver(sender=sender, clock=clock, outbox=outbox, only=escalation_id)


def deliver(sender=reporting.telegram, clock=reporting.now, outbox=OUTBOX, only=None):
    sent = 0
    with reporting.locked(Path(outbox).with_suffix('.lock')):
        items = list(state(outbox).values())
        due = [i for i in items if i['status'] not in {'delivered', 'deduped'}
               and (not i.get('next_retry') or reporting.parse(i['next_retry']) <= clock())]
        if only is None and len(due) > 8:
            hour_ago = clock() - timedelta(hours=1)
            recent = [i for i in items if (reporting.parse(i.get('created_at')) or hour_ago) >= hour_ago]
            ids = [i['id'] for i in (recent or due)]
            body = (f'[BÃO LỖI] {len(ids)} sự cố cần bạn quyết trong 1 giờ: '
                    + ', '.join(ids))
            try:
                code = sender(body)
            except Exception:
                code = 99
            for item in due:
                update = {**item, 'at': clock().isoformat(), 'tries': item['tries'] + 1,
                          'result_code': code, 'status': 'delivered' if code == 0 else 'pending',
                          'next_retry': None if code == 0 else
                          (clock() + timedelta(minutes=5)).isoformat()}
                reporting.append(outbox, update, durable=True)
                if code == 0:
                    try:
                        reporting.mark(Path(item['job_path']), item['attempt'], True)
                    except (OSError, KeyError):
                        pass
                    sent += 1
            return sent
        for item in items:
            if only and item['escalation_id'] != only:
                continue
            if item['status'] in {'delivered', 'deduped'}:
                continue
            when = reporting.parse(item.get('next_retry'))
            if when and when > clock():
                continue
            try:
                code = sender(item['body'])
            except Exception:
                code = 99
            tries = item['tries'] + 1
            update = {**item, 'at': clock().isoformat(), 'tries': tries,
                      'result_code': code, 'status': 'delivered' if code == 0 else 'pending',
                      'next_retry': None if code == 0 else
                      (clock() + timedelta(minutes=DELAYS[min(tries - 1, len(DELAYS)-1)])).isoformat()}
            reporting.append(outbox, update, durable=True)
            if code == 0:
                try:
                    reporting.mark(Path(item['job_path']), item['attempt'], True)
                except (OSError, KeyError):
                    pass
                sent += 1
    return sent


def pending_count(path=OUTBOX):
    return sum(x['status'] not in {'delivered', 'deduped'} for x in state(path).values())


def signature_counts(path=OUTBOX):
    counts = {}
    for row in state(path).values():
        counts[row.get('signature')] = counts.get(row.get('signature'), 0) + 1
    return counts
