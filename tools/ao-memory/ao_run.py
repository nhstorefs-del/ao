"""Checked dispatch into AO journal and a detached per-task supervisor."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import hidden_popen_kwargs, HERE, JOURNAL, atomic_json, read_json, redact, pid_alive, resolve_worker_command

JOBS = HERE / 'jobs'
WORKERS = HERE / 'workers.json'
sys.path.insert(0, str(JOURNAL))
import delegation_gate

def gate_journal(job): return Path(job.get('journal') or JOURNAL / 'journal.jsonl')

def job_gate(job, states=('DELEGATED',)):
    if not job.get('attempt_id','').startswith(job.get('id','')+'#'): raise delegation_gate.GateRefused('DELEGATION_JOB_ID_MISMATCH')
    return delegation_gate.receipt_check(gate_journal(job), job['attempt_id'], job.get('current_worker', job['worker']), job['brief'], states)

def cli(script, *args, queue=None, journal=None):
    cmd = [sys.executable, str(JOURNAL / script)]
    if queue is not None and script == 'queue.py': cmd += ['--queue', str(queue)]
    if journal is not None: cmd += ['--journal', str(journal)]
    cmd += list(map(str, args))
    p = subprocess.run(cmd, cwd=HERE, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', **hidden_popen_kwargs())
    if p.returncode:
        raise RuntimeError(f'{script} {args[0]} failed ({p.returncode}): {redact(p.stdout + p.stderr).strip()[:400]}')
    return p.stdout

def state(queue=None, journal=None):
    return json.loads(cli('queue.py', 'state', '--json', queue=queue, journal=journal))

def task(snapshot, ident):
    return next((x for x in snapshot['tasks'] if x['id'] == ident), None)

def config(name, path=WORKERS):
    if not Path(path).is_file():
        raise ValueError(f'thiếu {path}: copy workers.example.json thành workers.json rồi sửa cho máy của bạn (xem README)')
    c = read_json(path, {}).get(name)
    if not c: raise ValueError(f'worker không biết: {name}')
    if not c.get('enabled'): raise ValueError(f'worker tắt: {name}: {c.get("reason", "") }')
    command, _, _ = resolve_worker_command(c['command'])
    if not Path(command[0]).is_file(): raise ValueError(f'lệnh worker không tồn tại: {name}')
    if not Path(c['cwd']).is_dir(): raise ValueError(f'thư mục chạy worker không tồn tại: {name}')
    if c.get('key_file') and not Path(c['key_file']).is_file(): raise ValueError(f'thiếu tệp khoá: {name}')
    return c

def validate_signal(s):
    if s.startswith('file:'):
        raw = s[5:].strip().removesuffix(' exists').strip()
        return bool(raw) and Path(raw).is_absolute()
    return bool(re.fullmatch(r'cmd:.+\s+exit=-?\d+\s*', s, re.S))

def start(a):
    lane = str(getattr(a, 'lane', '') or '').upper()
    if lane not in 'ABCDEFG' or len(lane) != 1:
        label = 'THIẾU' if not lane else 'SAI'
        raise ValueError(label + ' --lane: chọn một chữ A-G (nhóm việc tự định nghĩa, ví dụ A = nền AO, B = nội dung, C = dữ liệu, D = website, E = dự án lẻ, F/G = tùy chọn)')
    sys.path.insert(0, str(HERE.parent / 'ao-report'))
    import maintenance
    warning = maintenance.warning()
    if warning: print(warning, file=sys.stderr)
    if os.environ.get('AO_AUTOREPORT', '1').lower() not in {'0', 'false', 'off'}:
        sys.path.insert(0, str(HERE.parent / 'ao-report'))
        import recovery_policy
        recovery_policy.verify_lock()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', a.id): raise ValueError('id không hợp lệ')
    brief = Path(a.brief).resolve()
    if not brief.is_file(): raise ValueError(f'brief không tồn tại: {brief}')
    if not a.signal or not all(validate_signal(s) for s in a.signal): raise ValueError('tiêu chí sai; cần file:<đường dẫn tuyệt đối> hoặc cmd:<lệnh> exit=N')
    if a.max_retries < 0: raise ValueError('max-retries phải >= 0')
    if task(state(a.queue, a.journal), a.id): raise ValueError(f'id đã tồn tại: {a.id}; dùng resume cho job cũ')
    jobdir = Path(a.jobs) / a.id
    if jobdir.exists(): raise ValueError(f'thư mục job đã tồn tại: {jobdir}')
    delegation_gate.prepare(a.journal or JOURNAL / 'journal.jsonl', a.id + '#1', a.worker, brief, 'ao-run')
    config(a.worker, a.workers)
    for w in a.fallback_worker: config(w, a.workers)
    args = ['add', '--id', a.id, '--title', a.title, '--workflow', a.workflow, '--worker', a.worker, '--brief', brief]
    for s in a.signal: args += ['--signal', s]
    for w in a.fallback_worker: args += ['--fallback-worker', w]
    for d in a.depends_on: args += ['--depends-on', d]
    cli('queue.py', *args, queue=a.queue, journal=a.journal)
    if not task(state(a.queue, a.journal), a.id): raise RuntimeError('đã add nhưng đọc lại không thấy task')
    cli('queue.py', 'sync', queue=a.queue, journal=a.journal)
    receipt = json.loads(cli('queue.py', 'dispatch', '--id', a.id, '--by', 'ao-run', queue=a.queue, journal=a.journal))
    attempt = receipt.get('attempt_id')
    if not attempt: raise RuntimeError('dispatch không trả attempt_id')
    jobdir.mkdir(parents=True)
    job = {'lane': lane, 'id': a.id, 'title': a.title, 'workflow': a.workflow, 'brief': str(brief),
           'signals': a.signal, 'worker': a.worker, 'fallback_workers': a.fallback_worker,
           'attempt_id': attempt, 'max_retries': a.max_retries, 'retries': 0,
           'queue': str(a.queue) if a.queue else None, 'journal': str(a.journal) if a.journal else None,
           'workers': str(a.workers), 'jobs': str(a.jobs),
           'data_class': a.data_class, 'work_type': a.work_type,
           'requires_owner_approval': a.requires_owner_approval,
           'required_metrics': a.required_metric}
    atomic_json(jobdir / 'job.json', job)
    (jobdir / 'worker.log').touch()
    (jobdir / 'events.jsonl').touch()
    pid = launch_supervisor(jobdir)
    if a.project:
        import registry
        registry.change(a.project, task=a.id)
    print(f'id={a.id} attempt_id={attempt} supervisor_pid={pid} log={jobdir / "worker.log"}')

def launch_supervisor(jobdir):
    job=read_json(Path(jobdir)/'job.json', {})
    if not job: raise delegation_gate.GateRefused('DELEGATION_JOB_MISSING')
    job_gate(job)
    p = subprocess.Popen([sys.executable, str(HERE / 'ao_supervisor.py'), str(jobdir)], cwd=HERE,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=os.name != 'nt', **hidden_popen_kwargs(detached=True, new_process_group=True))
    return p.pid

def effective_status(x):
    """Read-only view of a running job; keep the persisted state intact."""
    if x.get('state') != 'RUNNING': return x
    result = dict(x)
    result['supervisor_alive'] = pid_alive(x.get('supervisor_pid'), x.get('supervisor_created'))
    result['worker_alive'] = pid_alive(x.get('worker_pid'), x.get('worker_created'))
    try:
        changed = datetime.fromisoformat(x['last_log_change'].replace('Z', '+00:00'))
        result['log_age_minutes'] = max(0, round((datetime.now(timezone.utc) - changed).total_seconds()/60, 1))
    except (KeyError, AttributeError, TypeError, ValueError): result['log_age_minutes'] = None
    if not result['supervisor_alive']: result['state'] = 'MẤT_GIÁM_SÁT'
    return result

def wait(a):
    path = a.jobs / a.id
    if not (path / 'status.json').is_file():
        print(json.dumps({'id': a.id, 'state': 'NOT_FOUND', 'attempt_id': None,
                          'worker': None, 'retries': None, 'waited_seconds': 0.0,
                          'next': 'kiểm tra id'} , ensure_ascii=False))
        return 2
    started = time.monotonic()
    hints = {'DONE_NEEDS_REVIEW': 'kiểm kết quả rồi journal complete/accept',
             'FAILED': 'kiểm lỗi rồi chạy ao_run resume',
             'STOPPED': 'kiểm việc đã dừng',
             'ORPHANED': 'chạy ao_run resume',
             'MẤT_GIÁM_SÁT': 'chạy ao_run resume hoặc reap',
             'TIMEOUT': 'tiếp tục theo dõi bằng ao_run wait'}
    codes = {'DONE_NEEDS_REVIEW': 0, 'FAILED': 10, 'STOPPED': 11,
             'ORPHANED': 12, 'MẤT_GIÁM_SÁT': 12, 'TIMEOUT': 13}
    while True:
        raw = read_json(path / 'status.json', {})
        x = effective_status(raw)
        current = x.get('state')
        if current in ('GAVE_UP', 'SUPERVISOR_ERROR'):
            current = 'FAILED'
        elif current == 'FAILED':
            job = read_json(path / 'job.json', {})
            # FAILED is written before RETRY/GAVE_UP; a live supervisor may advance it.
            if x.get('retries', 0) < job.get('max_retries', 0) or pid_alive(x.get('supervisor_pid'), x.get('supervisor_created')):
                current = None
        if current in codes or time.monotonic() - started >= a.timeout:
            result = current if current in codes else 'TIMEOUT'
            print(json.dumps({'id': a.id, 'state': result,
                              'attempt_id': x.get('attempt_id'), 'worker': x.get('worker'),
                              'retries': x.get('retries'),
                              'waited_seconds': round(time.monotonic() - started, 3),
                              'next': hints[result], **({'error': x['error']} if x.get('error') else {})}, ensure_ascii=False))
            return codes[result]
        time.sleep(min(a.poll, max(0, a.timeout - (time.monotonic() - started))))

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    p = argparse.ArgumentParser()
    p.add_argument('--queue', type=Path); p.add_argument('--journal', type=Path)
    p.add_argument('--workers', type=Path, default=WORKERS); p.add_argument('--jobs', type=Path, default=JOBS)
    sub = p.add_subparsers(dest='action', required=True)
    s = sub.add_parser('start')
    s.add_argument('--lane')
    for x in ('id','title','workflow','worker','brief'): s.add_argument('--'+x, required=True)
    s.add_argument('--signal', action='append', required=True); s.add_argument('--fallback-worker', action='append', default=[])
    s.add_argument('--depends-on', action='append', default=[]); s.add_argument('--project'); s.add_argument('--max-retries', type=int, default=2)
    s.add_argument('--data-class', choices=['public','internal','sensitive'], default='sensitive')
    s.add_argument('--work-type', choices=['code','simple'], default='code')
    s.add_argument('--requires-owner-approval', action='store_true')
    s.add_argument('--required-metric', action='append', default=[])
    for x in ('status','stop','resume'):
        q=sub.add_parser(x); q.add_argument('--id', required=x!='status')
        if x=='resume':
            q.add_argument('--recover-delegation', action='store_true')
            q.add_argument('--recovery-reason')
    q=sub.add_parser('wait'); q.add_argument('--id', required=True)
    q.add_argument('--timeout', type=float, default=3600); q.add_argument('--poll', type=float, default=5)
    q=sub.add_parser('reap'); target=q.add_mutually_exclusive_group(required=True)
    target.add_argument('--id'); target.add_argument('--all', action='store_true')
    sub.add_parser('list'); sub.add_parser('doctor')
    sub.add_parser('report-pending')
    sub.add_parser('ao-status')
    a=p.parse_args()
    try:
        if a.action=='wait':
            if a.timeout < 0 or a.poll <= 0: raise ValueError('timeout phải >= 0 và poll phải > 0')
            return wait(a)
        if a.action=='start': start(a)
        elif a.action=='doctor':
            for name,c in read_json(a.workers, {}).items():
                resolved, version, candidates = resolve_worker_command(c.get('command', []))
                command=bool(resolved) and Path(resolved[0]).is_file()
                if version:
                    print(f'{name}: selected exe={resolved[0]} version={version}')
                    for exe, found_version in candidates:
                        if exe != resolved[0]: print(f'{name}: candidate exe={exe} version={found_version}')
                    if '-alpha.' in version: print(f'{name}: CẢNH BÁO: bản Codex được chọn là alpha')
                key=not c.get('key_file') or Path(c['key_file']).is_file()
                print(f'{name}: enabled={c.get("enabled")} command={command} key={key} usable={bool(c.get("enabled") and command and key)}')
            print(f'journal_readable={bool(state(a.queue,a.journal))} jobs_writable={os.access(a.jobs if a.jobs.exists() else a.jobs.parent, os.W_OK)}')
        elif a.action=='report-pending':
            sys.path.insert(0, str(HERE.parent / 'ao-report'))
            import reporting
            for item in reporting.pending(a.jobs):
                print(f"{item['id']} {item['attempt']} | {item['state']} | {item['age_minutes']} phút | đã báo={item['owner_notified']} | {item['title']}")
        elif a.action=='ao-status':
            sys.path.insert(0, str(HERE.parent / 'ao-report'))
            import ao_status
            return ao_status.main([])
        elif a.action in ('list','status'):
            paths = [a.jobs/a.id] if getattr(a,'id',None) else sorted(a.jobs.glob('*')) if a.jobs.exists() else []
            for path in paths:
                x=read_json(path/'status.json', {})
                if x: print(json.dumps({**effective_status(x), 'lane': read_json(path/'job.json', {}).get('lane') or '?'},ensure_ascii=False))
        elif a.action=='reap':
            from ao_supervisor import event, status
            paths = sorted(a.jobs.glob('*')) if a.all and a.jobs.exists() else [a.jobs/a.id] if a.id else []
            if a.id and not (paths[0]/'status.json').exists(): raise ValueError('job không tồn tại')
            for path in paths:
                x=read_json(path/'status.json',{})
                if x.get('state')!='RUNNING' or pid_alive(x.get('supervisor_pid'),x.get('supervisor_created')): continue
                event(path,'ORPHANED','máy tắt/giám sát mất; reap không giao lại việc')
                status(path,state='ORPHANED')
                print(f'ORPHANED: {path.name}')
        elif a.action=='stop':
            path=a.jobs/a.id; x=read_json(path/'status.json',{})
            if not x: raise ValueError('job không tồn tại')
            atomic_json(path/'stop.json', {'reason':'Owner/AO dừng'})
            print(f'stop requested: {a.id}')
        elif a.action=='resume':
            path=a.jobs/a.id; x=read_json(path/'status.json',{})
            if not (path/'job.json').exists(): raise ValueError('job không tồn tại')
            if task(state(a.queue,a.journal),a.id)['status']=='accepted': raise ValueError('task đã accepted')
            pid=x.get('supervisor_pid')
            if pid and pid_alive(pid,x.get('supervisor_created')): raise ValueError(f'giám sát đang chạy pid={pid}')
            job=read_json(path/'job.json',{})
            if a.recover_delegation:
                if x.get('worker_pid') and pid_alive(x['worker_pid'],x.get('worker_created')): raise delegation_gate.GateRefused('RECOVERY_WORKER_STILL_ALIVE')
                delegation_gate.recover(gate_journal(job), job['attempt_id'], job.get('current_worker',job['worker']), job['brief'], a.recovery_reason)
            print(f'supervisor_pid={launch_supervisor(path)}')
    except Exception as e:
        print(f'ERROR: {redact(e)}', file=sys.stderr); return 2
    return 0

if __name__=='__main__': raise SystemExit(main())
