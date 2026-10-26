"""Independent checker: invokes telegram-send directly, without watchdog/reporting sender."""
import json
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEART = HERE/'heartbeat.json'
STATE = HERE/'checker_state.json'


def run(now=None, sender=None, heart=HEART, state=STATE):
    if os.environ.get('AO_AUTOREPORT','1').lower() in {'0','false','off'}:
        return {'disabled': True}
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(timezone(timedelta(hours=7)))
    if not 7 <= local.hour < 22:
        return {'active_hours': False}
    try:
        hb = json.loads(Path(heart).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        hb = {}
    problems = []
    for component in ('watchdog', 'digest'):
        try:
            at = datetime.fromisoformat(hb[component]['at'])
            if now - at > timedelta(minutes=45 if component == 'watchdog' else 12*60):
                problems.append(f'bộ gác ngừng ({component}) từ {at.astimezone(local.tzinfo):%d/%m %H:%M}')
        except (KeyError, ValueError, TypeError):
            problems.append(f'bộ gác ({component}) chưa có nhịp tim')
    # An idle day has no job events. A supervisor claiming active work today
    # while JOBS.log has no event today is a ledger alarm.
    supervisor = hb.get('supervisor', {})
    try:
        supervisor_at = datetime.fromisoformat(supervisor['at'])
        if supervisor_at.astimezone(local.tzinfo).date() == local.date() and int(supervisor.get('watched',0)) > 0:
            job_log = HERE/'JOBS.log'
            latest_line = job_log.read_text(encoding='utf-8').splitlines()[-1] if job_log.exists() else ''
            last_at = datetime.fromisoformat(latest_line.split(' ',1)[0]) if latest_line else None
            if not last_at or last_at.astimezone(local.tzinfo).date() != local.date():
                problems.append('JOBS.log không ghi sự kiện trong ngày có worker')
    except (KeyError, ValueError, OSError, IndexError):
        problems.append('JOBS.log hoặc nhịp tim supervisor không đọc được')
    outbox = HERE/'outbox'/'human.jsonl'
    latest = {}
    if outbox.exists():
        for line in outbox.read_text(encoding='utf-8').splitlines():
            try:
                item = json.loads(line); latest[item['escalation_id']] = item
            except (ValueError, KeyError):
                pass
    old = [x for x in latest.values() if x.get('status') != 'delivered' and
           now - datetime.fromisoformat(x['created_at']) > timedelta(minutes=30)]
    if old:
        problems.append(f'{len(old)} tin cần người chưa gửi >30 phút')
    if not problems:
        return {'healthy': True}
    try:
        previous = json.loads(Path(state).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    if previous.get('problems') == problems and previous.get('at') and now - datetime.fromisoformat(previous['at']) < timedelta(hours=3):
        return {'deduped': True, 'problems': problems}
    body = '[GIÁM SÁT] ' + '; '.join(problems)
    if sender is None:
        def sender(text):
            outbox = os.environ.get('AO_REPORT_TEST_OUTBOX')
            if outbox and os.environ.get('AO_REPORT_SELFTEST') == '1':
                row = {'at': datetime.now(timezone.utc).isoformat(),
                       'sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text}
                Path(outbox).parent.mkdir(parents=True, exist_ok=True)
                with Path(outbox).open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                return 0
            from telegram_mute import record_if_muted
            if record_if_muted(text, HERE):
                return 0
            return subprocess.run([sys.executable, str(HERE.parent/'telegram-send'/'telegram_send.py'), '--text', text],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=75, check=False).returncode
    code = sender(body)
    if code == 0:
        Path(state).write_text(json.dumps({'at': now.isoformat(), 'problems': problems}, ensure_ascii=False), encoding='utf-8')
    return {'healthy': False, 'sent': code == 0, 'problems': problems}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
