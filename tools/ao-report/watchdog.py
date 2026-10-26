"""Run every 30 minutes from Task Scheduler; never requires an AI session."""
import hashlib
import json
import sys
import subprocess
from datetime import timedelta
from pathlib import Path

import reporting
import human_outbox
import heartbeat
import digest as digest_module
import normal_outbox

sys.path.insert(0, str(reporting.HERE / 'coverage'))
try:
    import coverage_lib
except Exception:
    coverage_lib = None


def _coverage_counts(clock):
    """Read-only coverage numbers for the run summary (no extra sends here)."""
    if coverage_lib is None:
        return {}
    try:
        return coverage_lib.summary(clock())
    except Exception:
        return {}


def run(jobs=reporting.JOBS, sender=reporting.telegram, sent=reporting.SENT, clock=reporting.now,
        legacy_file=None, reminder_file=None):
    try:
        checker = Path(__file__).resolve().parents[1] / 'ao-intake' / 'integrity' / 'integrity_check.py'
        result = subprocess.run([sys.executable, str(checker), 'check', '--brief'],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        if result.returncode != 0:
            log = checker.parent / 'watchdog.log'
            with log.open('a', encoding='utf-8') as stream:
                stream.write((result.stdout.strip() or 'C?NH B?O C?U H?NH: b? ki?m hash l?i') + '\n')
    except Exception as exc:
        try:
            log = Path(__file__).resolve().parents[1] / 'ao-intake' / 'integrity' / 'watchdog.log'
            with log.open('a', encoding='utf-8') as stream:
                stream.write('C?NH B?O C?U H?NH: kh?ng ch?y ???c b? ki?m hash: ' + str(exc) + '\n')
        except OSError:
            pass
    if not reporting.enabled():
        return {'disabled': True}
    coverage = _coverage_counts(clock)
    reporting.repair_job_log()
    heartbeat.beat('watchdog', watched=sum(1 for _ in Path(jobs).glob('*')), clock=clock)
    human_outbox.deliver(sender=sender, clock=clock)
    if Path(jobs) == reporting.JOBS:
        digest_module.run(sender=sender, clock=clock)
    legacy_file = Path(legacy_file or reporting.HERE / 'legacy.json')
    reminder_file = Path(reminder_file or reporting.HERE / 'reminders.jsonl')
    legacy = {}
    for path in Path(jobs).glob('*'):
        state = reporting.read(path / 'status.json')
        attempt = state.get('attempt_id')
        if not attempt or state.get('state') not in reporting.STATES:
            continue
        if reporting.legacy(path, attempt):
            legacy[path.name] = attempt
        else:
            reporting.notify(path, sender=sender, sent=sent, clock=clock)
    if Path(jobs) == reporting.JOBS and sender is reporting.telegram:
        normal_outbox.flush(sender=sender, clock=clock)
    legacy_file.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding='utf-8')
    overdue = [item for item in reporting.pending(jobs, sent, clock) if item['age_minutes'] > 60]
    if not overdue:
        return {'legacy': len(legacy), 'overdue': 0, 'coverage': coverage}
    digest = hashlib.sha256('\n'.join(sorted(x['attempt'] for x in overdue)).encode()).hexdigest()
    with reporting.locked(reminder_file.with_suffix('.lock')):
        previous = reporting.rows(reminder_file)
        last = previous[-1] if previous else None
        if last and last.get('ok') and last.get('digest') == digest and (reporting.parse(last.get('at')) or clock()) + timedelta(hours=3) > clock():
            return {'legacy': len(legacy), 'overdue': len(overdue), 'reminder': 'suppressed', 'coverage': coverage}
        names = ', '.join(reporting.clean(x['title'], 50) for x in overdue[:8])
        if len(overdue) > 8:
            names += f' và {len(overdue)-8} việc khác'
        body = f'Còn {len(overdue)} việc xong chờ kiểm: {names}'
        try:
            code = sender(body)
        except Exception:
            code = 99
        reporting.append(reminder_file, {'at': clock().isoformat(), 'digest': digest, 'result_code': code,
                                         'ok': code == 0})
        return {'legacy': len(legacy), 'overdue': len(overdue), 'reminder': 'sent' if code == 0 else 'failed',
                'coverage': coverage}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
