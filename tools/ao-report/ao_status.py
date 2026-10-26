"""Short AO warning view; safe to run without an AI session."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import subprocess
import argparse
import maintenance

import reporting
import human_outbox
import heartbeat
import run_counter

sys.path.insert(0, str(Path(__file__).resolve().parent / 'coverage'))
import coverage_lib


def collect(jobs=reporting.JOBS, clock=reporting.now, lane=None):
    warnings=[]
    flag = maintenance.warning()
    if flag: warnings.append(flag)
    try:
        checker = Path(__file__).resolve().parents[1] / 'ao-intake' / 'integrity' / 'integrity_check.py'
        result = subprocess.run([sys.executable, str(checker), 'check', '--brief'],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        if result.returncode in (8, 9):
            warnings.extend(result.stdout.strip().splitlines()[:3])
        elif result.returncode != 0:
            warnings.append('C?NH B?O C?U H?NH: kh?ng ch?y ???c b? ki?m hash')
    except Exception:
        warnings.append('C?NH B?O C?U H?NH: kh?ng ch?y ???c b? ki?m hash')
    lane = lane.upper() if lane is not None else None
    unsent = human_outbox.pending_count()
    if unsent:
        warnings.append(f'CHƯA GỬI ĐƯỢC: {unsent} tin cần người')
    local = clock().astimezone(reporting.BASELINE.tzinfo)
    if 7 <= local.hour < 22:
        hb = reporting.read(heartbeat.PATH)
        stamp = reporting.parse(hb.get('watchdog', {}).get('at'))
        if not stamp or (clock()-stamp).total_seconds() > 2700:
            warnings.append('BỘ GÁC CHẾT? watchdog quá 45 phút không có nhịp tim')
    for path in jobs.glob('*'):
        status=reporting.read(path/'status.json')
        job=reporting.read(path/'job.json')
        job_lane = job.get('lane') or '?'
        if lane is not None and job_lane != lane: continue
        first_job_warning = len(warnings)
        attempt=status.get('attempt_id')
        if not attempt or reporting.legacy(path,attempt) or path.name in reporting.closed_jobs():
            continue
        try:
            key, count = run_counter.lookup(job)
            total = count['runs_total']
            if total and status.get('state') in {'BLOCKED','GAVE_UP','STALLED','MAX_RUNTIME','ORPHANED','SUPERVISOR_ERROR'}:
                trail = ', '.join(f"lần {i} {run.get('type')}" for i, run in enumerate(count.get('runs', []), 1))
                warnings.append(f'{path.name}: đã chạy {total}/3 ({trail[:110]})')
            if total >= 3 and status.get('state') != 'DONE_NEEDS_REVIEW':
                warnings.append(f'{path.name}: đã chạm trần 3/3 ⇒ cần người')
        except (OSError, ValueError, KeyError):
            warnings.append(f'{path.name}: không đọc được bộ đếm lần chạy')
        state=status.get('state')
        start=reporting.parse(status.get('started_at'))
        finish=reporting.parse(status.get('finished_at'))
        age=(clock()-start).total_seconds()/60 if start else 0
        if state=='RUNNING':
            config=reporting.read(job.get('workers','')) if job.get('workers') else {}
            worker=config.get(status.get('worker'),{})
            limit=float(worker.get('max_runtime_minutes') or 60)
            if age>limit: warnings.append(f'RUNNING quá hạn {path.name} {age:.0f} phút')
        elif state in {'BLOCKED','GAVE_UP','STALLED','MAX_RUNTIME','ORPHANED','SUPERVISOR_ERROR'} or status.get('blocked'):
            warnings.append(f'BỊ CHẶN {path.name} {attempt}: {reporting.clean(status.get("reason") or state,100)}')
        if state in reporting.STATES:
            sent=reporting.latest(path,attempt)
            notified=bool(sent and sent.get('ok'))
            if not notified: warnings.append(f'CHƯA BÁO Owner {path.name} {attempt}')
            if finish and (clock()-finish).total_seconds()>3600:
                journal=job.get('journal') or reporting.HERE.parent/'ao-journal'/'journal.jsonl'
                if path.name not in reporting.accepted_ids(journal):
                    warnings.append(f'CHƯA NGHIỆM THU {path.name} {(clock()-finish).total_seconds()/60:.0f} phút')
        warnings[first_job_warning:] = [line + f' · làn {job_lane}' for line in warnings[first_job_warning:]]
    try:
        state = coverage_lib.items()
        stale = coverage_lib.stale(state, clock())
        for item in stale['unreported']:
            label = coverage_lib.clean(item.get('desc') or item['id'], 50)
            warnings.append(f'NGOÀI ao_run: xong chưa báo Owner · {label} [{item["id"]}]')
        for item in stale['open']:
            label = coverage_lib.clean(item.get('desc') or item['id'], 50)
            warnings.append(f'NGOÀI ao_run: nghi treo · {label} [{item["id"]}]')
        background = [x for x in state.values() if x['kind'] == 'bg' and x['finished_at'] is None]
        if background:
            warnings.append(f'NGOÀI ao_run: {len(background)} lệnh nền đã ghi nhận (không theo dõi hoàn thành)')
    except Exception:
        pass
    return warnings


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--lane', type=str.upper, choices=list('ABCDEFG'))
    args = parser.parse_args(argv)
    warnings=collect(lane=args.lane)
    print('AO STATUS: '+ ('CÓ CẢNH BÁO' if warnings else 'sạch'))
    for item in warnings[:23]: print(item)
    if len(warnings)>23: print(f'... và {len(warnings)-23} cảnh báo khác')
    return 1 if warnings else 0


if __name__=='__main__': raise SystemExit(main())
