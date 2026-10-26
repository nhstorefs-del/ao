"""Scheduled digest, including an explicit healthy message when there are no issues."""
import argparse
import json
from datetime import timedelta
from pathlib import Path

import reporting
import heartbeat
import human_outbox
import error_classifier

LOG = reporting.HERE / 'digest.jsonl'

import sys as _sys
_sys.path.insert(0, str(reporting.HERE / 'coverage'))
try:
    import coverage_lib
    import scheduled_status
except Exception:  # never let the coverage add-on break the digest
    coverage_lib = None
    scheduled_status = None


def run(sender=reporting.telegram, clock=reporting.now, log=LOG, force=False):
    if not reporting.enabled():
        return {'disabled': True}
    local = clock().astimezone(reporting.BASELINE.tzinfo)
    slot = '08:00' if local.hour < 17 or local.hour == 17 and local.minute < 30 else '17:30'
    if local.hour < 8:
        return {'due': False}
    in_window = local.hour == 8 or (local.hour == 17 and local.minute >= 30)
    prior = reporting.read(heartbeat.PATH)
    stale = False
    watchdog_at = reporting.parse(prior.get('watchdog', {}).get('at'))
    if watchdog_at and clock() - watchdog_at > timedelta(minutes=30):
        stale = True
    if not force and not in_window and not stale:
        return {'due': False}
    key = f'{local.date()} {slot}'
    if not force and any(r.get('slot') == key and r.get('ok') for r in reporting.rows(log)):
        return {'due': False, 'already_sent': True}
    hb = heartbeat.beat('digest', clock=clock)
    pending = reporting.pending(clock=clock)
    human = human_outbox.pending_count()
    done = sum(reporting.read(p/'status.json').get('state') == 'DONE_NEEDS_REVIEW'
               for p in reporting.JOBS.glob('*'))
    blocked = sum(1 for p in reporting.JOBS.glob('*')
                  if reporting.read(p/'status.json').get('state') in {'BLOCKED','FAILED','STALLED','ORPHANED'})
    body = f'Mọi việc ổn · {done} việc xong · {blocked} lỗi · {len(pending)} việc chờ kiểm · {human} tin cần gửi bù\nBộ gác sống (nhịp tim {local:%H:%M}) · lần cuối kiểm: {local:%d/%m %H:%M}'
    wd = reporting.parse(hb.get('watchdog', {}).get('at'))
    if wd and clock() - wd > timedelta(minutes=45):
        body += f'\nMáy/bộ gác không hoạt động từ {wd.astimezone(reporting.BASELINE.tzinfo):%d/%m %H:%M} đến {local:%d/%m %H:%M}'
    if human:
        body += '\nCó việc cần bạn quyết; xem needs_human.md.'
    noise = [r for r in reporting.rows(reporting.HERE/'noise.jsonl')
             if (reporting.parse(r.get('at')) or local).astimezone(reporting.BASELINE.tzinfo).date() == local.date()]
    items = sum(int(r.get('items', 0)) for r in noise)
    msgs = sum(int(r.get('messages', 0)) for r in noise)
    if items > msgs and items:
        body += f'\nđã giảm nhiễu: gộp {items} thông báo vào {msgs} tin'
    counts = human_outbox.signature_counts()
    top = max(counts.items(), key=lambda kv: kv[1], default=None)
    if top and top[1] > 1:
        body += f'\n{top[0]} lặp {top[1]} lần'
    learned = json.loads(error_classifier.PATTERNS.read_text(encoding='utf-8')).get('learned', {})
    if learned and local.weekday() == 0 and slot == '08:00':
        signatures = ', '.join(sorted(learned)) or 'không có'
        body += f'\nTUẦN — chữ ký lạ đã học/chờ xác nhận: {signatures}'
    if local.weekday() == 0 and slot == '08:00':
        from collections import Counter
        weekly = Counter()
        for row in reporting.rows(reporting.UNKNOWN):
            at = reporting.parse(row.get('at'))
            if at and (clock() - at).days <= 7:
                weekly[row.get('signature')] += 1
        repeated = [s for s in sorted(weekly) if weekly[s] >= 3]
        if repeated:
            body += ('\nđề xuất sửa gốc: '
                     + ', '.join(f'{s} ({weekly[s]} lần/tuần)' for s in repeated))
    awaiting = [x for x in learned.values() if x.get('state') == 'PENDING_CONFIRMATION']
    if awaiting:
        body += f'\nCHỜ XÁC NHẬN ({len(awaiting)}):'
        for item in awaiting:
            body += (f"\n{item.get('id')}: {item.get('action')} · {item.get('approved_at')} · "
                     f"đã áp dụng {item.get('applied_count', 0)} lần\n"
                     f"Owner đã nói: {item.get('approved_by_owner_quote')}\n"
                     f"learn.py reject {item.get('id')} / learn.py confirm {item.get('id')} --owner-quote ...")
    recovery_rows = reporting.rows(reporting.RECOVERY)
    cost = sum(float(r.get('estimated_cost_usd', 0)) for r in recovery_rows
               if (reporting.parse(r.get('at')) or clock()).astimezone(reporting.BASELINE.tzinfo).date() == local.date())
    body += f'\nchi phí ước tính tự khắc phục hôm nay: ${cost:.2f} (thông tin, không chặn)'
    previous=[]
    for days_ago in range(1,8):
        day=local.date()-timedelta(days=days_ago)
        previous.append(sum(float(r.get('estimated_cost_usd',0)) for r in recovery_rows
                            if reporting.parse(r.get('at')) and
                            reporting.parse(r['at']).astimezone(reporting.BASELINE.tzinfo).date()==day))
    average=sum(previous)/7
    if average>0 and cost>=3*average:
        body += f'\ncảnh báo vàng: chi phí ước tính cao gấp {cost/average:.1f} lần trung bình 7 ngày (không chặn)'
    if coverage_lib is not None:
        try:
            cov = coverage_lib.summary(clock())
            if cov['unreported'] or cov['suspect_hang']:
                detail = ''
                if cov['latest_unreported']:
                    detail += ' · xong chưa báo: ' + ', '.join(cov['latest_unreported'])
                if cov['latest_open']:
                    detail += ' · nghi treo: ' + ', '.join(cov['latest_open'])
                body += (f"\nNGOÀI ao_run: {cov['unreported']} mục xong chưa báo Owner"
                         f" · {cov['suspect_hang']} mục nghi treo{detail}")
            if scheduled_status is not None:
                for line in scheduled_status.digest_lines(clock()):
                    body += '\n' + line
        except Exception:
            pass
    try:
        code = sender(body)
    except Exception:
        code = 99
    reporting.append(log, {'slot': key, 'at': clock().isoformat(), 'ok': code == 0,
                           'result_code': code, 'body_sha256': __import__('hashlib').sha256(body.encode()).hexdigest()}, durable=True)
    return {'due': True, 'ok': code == 0, 'slot': key}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
