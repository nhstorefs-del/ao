"""One detached supervisor per job; all ledger writes go through official CLIs."""
import json
import hashlib
import os
import re
import subprocess
import sys
import time
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from common import hidden_popen_kwargs, atomic_json, read_json, redact, process_start_time, resolve_worker_command
from ao_run import cli, state, task, job_gate, gate_journal, delegation_gate

def stamp(): return datetime.now(timezone.utc).isoformat()

def event(path, kind, detail='', **fields):
    with (path/'events.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps({'ts':stamp(),'type':kind,'detail':redact(detail), **fields},ensure_ascii=False)+'\n')

def status(path, **values):
    old=read_json(path/'status.json',{})
    old.update(values)
    if values.get('state') == 'RUNNING':
        old['owner_notified'] = False
        old['started_at'] = stamp()
    if values.get('state') in REPORT_STATES:
        old['finished_at'] = stamp()
    atomic_json(path/'status.json',old)
    if values.get('state') in REPORT_STATES:
        try:
            _reporting().notify(path, values['state'])
        except Exception as exc:
            event(path, 'REPORT_ERROR', type(exc).__name__)


REPORT_STATES = {'DONE_NEEDS_REVIEW','FAILED','BLOCKED','STOPPED','STALLED','ORPHAN','ORPHANED','MAX_RUNTIME','GAVE_UP','SUPERVISOR_ERROR'}


def _reporting():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ao-report'))
    import reporting
    return reporting


def _classifier():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ao-report'))
    import error_classifier
    return error_classifier


def _policy():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ao-report'))
    import recovery_policy
    return recovery_policy

def _counter():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ao-report'))
    import run_counter
    return run_counter

def _health():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ao-report'))
    import worker_health
    return worker_health


def log_job(job, worker, kind, note=''):
    if not _reporting().enabled():
        return
    try:
        _reporting().job_event(job, worker, kind, note)
        import heartbeat
        heartbeat.beat('supervisor', watched=1)
    except Exception:
        pass  # Reporting must never change worker execution.

def key_value(c):
    if not c.get('key_file'): return None
    name=c['key_var']
    for line in Path(c['key_file']).read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if line.startswith('export '): line=line[7:]
        if line.startswith(name+'='): return line.split('=',1)[1].strip().strip('"\'')
    raise RuntimeError(f'tệp khoá thiếu biến {name}')

def files(job):
    out=[]
    for s in job['signals']:
        if s.startswith('file:'):
            out.append(Path(s[5:].strip().removesuffix(' exists').strip()))
    return out

def signatures(paths):
    out=[]
    for p in paths:
        try:
            st=p.stat(); out.append((str(p),st.st_size,st.st_mtime_ns))
        except OSError: out.append((str(p),None,None))
    return out

def progress_files(brief):
    """Progress notes next to the brief are optional and may appear mid-run."""
    try:
        with os.scandir(Path(brief).parent) as entries:
            return sorted([Path(entry.path) for entry in entries
                    if not entry.is_symlink() and entry.is_file(follow_symlinks=False)
                    and entry.name.upper().startswith('PROGRESS')
                    and entry.name.lower().endswith('.txt')])
    except OSError:
        return []

def workspace_roots(job, workspace_root, warn=None):
    """Select only declared write areas; fall back when none can be identified."""
    workspace_root=Path(workspace_root).resolve()
    roots=[]
    brief=Path(job.get('brief',''))
    if brief.is_file():
        roots.append(brief.resolve().parent)
        lines=brief.read_text(encoding='utf-8-sig',errors='replace').splitlines()
        in_scope=False
        for line in lines:
            if re.match(r'^\s*#{1,6}\s+',line):
                in_scope=bool(re.search(r'phạm vi ghi|pham vi ghi',line,re.I))
            inline_scope=re.match(r'^\s*(?:phạm vi ghi|pham vi ghi|chỉ ghi|chi ghi)\s*:',line,re.I)
            if not (in_scope or inline_scope): continue
            # Bare paths must be absolute and end at a slash or file extension.
            quoted=re.findall(r'`([^`]+)`',line)
            bare_line=re.sub(r'`[^`]*`',' ',line)
            bare=re.findall(r'[A-Za-z]:[\\/][^`<>*?"|,;()\r\n]*[\\/]|[A-Za-z]:[\\/][^`<>*?"|,;()\r\n]*?\.[A-Za-z0-9]{1,8}(?=\s|$|[,;()])',bare_line)
            for raw in quoted+bare:
                raw=raw.strip().rstrip('\\/')
                if not raw or any(ch in raw for ch in '<>*?"|'): continue
                if '..' in re.split(r'[\\/]+',raw): continue
                candidate=Path(raw)
                if not candidate.is_absolute() and not any(ch in raw for ch in '/\\'):
                    continue
                if not candidate.is_absolute(): candidate=workspace_root/candidate
                try:
                    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
                        continue
                    candidate=candidate.resolve(strict=False)
                    candidate.relative_to(workspace_root)
                except (OSError, ValueError):
                    continue
                folder=candidate if candidate.is_dir() or not candidate.suffix else candidate.parent
                if folder!=workspace_root and folder.is_dir(): roots.append(folder)
    for signal_file in files(job):
        try:
            folder=signal_file.resolve(strict=False).parent
            if folder.is_dir(): roots.append(folder)
        except OSError:
            continue
    if not roots:
        if warn: warn('WORKSPACE_SCOPE_FALLBACK','Không xác định được vùng quét; dùng gốc repo với trần 100000 mục')
        return [workspace_root], True
    # Drop descendants of another selected root to avoid duplicate scans.
    selected=[]
    for root in sorted(set(roots),key=lambda p:len(p.parts)):
        if not any(root==parent or parent in root.parents for parent in selected):
            selected.append(root)
    return selected, False

def recent_scoped_change(roots, since_ns, fallback=False):
    return max((recent_workspace_change(root,since_ns,
                 max_depth=6 if fallback else 16,
                 max_entries=100000 if fallback else 50000) for root in roots),default=0)

def recent_workspace_change(root, since_ns, max_depth=6, max_entries=20000):
    """Return newest changed file mtime; never follow links or scan vendor/job trees."""
    pending=deque([(Path(root), 0)])
    excluded={'.git', '__pycache__', 'jobs', 'node_modules'}
    newest=0
    examined=0
    while pending and examined < max_entries:
        folder, depth=pending.popleft()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    examined+=1
                    if examined > max_entries: break
                    if entry.is_symlink() or entry.name.lower() in excluded: continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            mtime=entry.stat(follow_symlinks=False).st_mtime_ns
                            if mtime > since_ns: newest=max(newest,mtime)
                        elif depth < max_depth and entry.is_dir(follow_symlinks=False):
                            pending.append((entry.path,depth+1))
                    except OSError:
                        continue
        except OSError:
            continue
    return newest

def stall_seconds(config):
    """Optional minute override; legacy seconds remain the fallback."""
    minutes=config.get('stall_minutes')
    return float(minutes)*60 if minutes is not None else config['stall_seconds']

def is_stalled(last_activity, now, config):
    return now-last_activity >= stall_seconds(config)

def stop_cause(last_activity, now, started, config):
    """Runtime cap wins even when recent activity would suppress STALL."""
    limit=config.get('max_runtime_minutes')
    if limit is not None and now-started >= float(limit)*60: return 'MAX_RUNTIME'
    if is_stalled(last_activity,now,config): return 'STALL'
    return None

def signals_pass(job):
    return not failed_signals(job)

def failed_signals(job):
    import re
    missing=[]
    for signal in job['signals']:
        if signal.startswith('file:'):
            target=Path(signal[5:].strip().removesuffix(' exists').strip())
            if not target.is_file() or target.stat().st_size == 0: missing.append(signal)
            elif target.suffix.lower()=='.json':
                try:
                    data=json.loads(target.read_text(encoding='utf-8-sig'))
                    if isinstance(data,dict) and str(data.get('status','')).lower() in {'fail','failed','error','blocked'}:
                        missing.append('qc:status='+str(data['status']))
                    for metric in job.get('required_metrics',[]):
                        value=data.get(metric) if isinstance(data,dict) else None
                        if not isinstance(value,(int,float)) or isinstance(value,bool):
                            missing.append('qc:thiếu số đo '+metric)
                except (OSError, ValueError):
                    missing.append('qc:JSON không hợp lệ '+str(target))
        else:
            missing.append('COMMAND_SIGNAL_DISABLED_BY_DELEGATION_GATE')
    return missing

def pattern_hint(tail, patterns):
    for line in reversed(tail.splitlines()):
        for pattern in patterns:
            position=line.lower().find(pattern.lower()) if pattern else -1
            if position >= 0:
                start=max(0, position-60)
                excerpt=line[start:start+180].strip()
                return f'mẫu {pattern!r}, dòng log: {excerpt}'
    return None

def kill_tree(p):
    if p.poll() is not None: return
    if os.name=='nt': subprocess.run(['taskkill','/T','/F','/PID',str(p.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, **hidden_popen_kwargs())
    else: p.kill()

def stop_for_max_runtime(path, process, minutes):
    reason='MAX_RUNTIME: vượt trần tổng thời gian '+str(minutes)+' phút'
    event(path,'MAX_RUNTIME',reason,limit_minutes=minutes)
    kill_tree(process)
    return reason

def next_attempt(path,job,reason,worker=None,retry_note=''):
    if job['retries']>=job['max_retries']:
        event(path,'GAVE_UP',reason)
        cli('journal.py','checkpoint','--task-id',job['id'],'--by','ao-run','--next-action','Owner kiểm tra và quyết định retry','--blocked-reason',f'NEEDS_OWNER: {reason}',journal=job['journal'])
        return False
    job['retries']+=1
    fallback=job['fallback_workers']
    # Switch to the next declared fallback, then stay there for later retries.
    index=min(job['retries']-1,len(fallback)-1)
    worker=worker or (fallback[index] if fallback else job['worker'])
    args=['dispatch','--id',job['id'],'--retry','--reason',f'auto: {reason}','--by','ao-run']
    if worker!=job['worker']: args+=['--worker',worker]
    receipt=json.loads(cli('queue.py',*args,queue=job['queue'],journal=job['journal']))
    job['attempt_id']=receipt['attempt_id']; job['current_worker']=worker
    if worker != job['worker']:
        job['fallback_switches'] = int(job.get('fallback_switches', 0)) + 1
    job['retry_note']=retry_note
    atomic_json(path/'job.json',job)
    event(path,'RETRY',f'{job["attempt_id"]} worker={worker} reason={reason}')
    return True

def run(path, secrets=None):
    secrets = secrets if secrets is not None else []
    job=read_json(path/'job.json',{})
    if not job: raise RuntimeError('job.json không tồn tại')
    job_gate(job)
    ident=job['id']
    ledger=task(state(job['queue'],job['journal']),ident)
    if not ledger or ledger['status']=='accepted': return
    old=read_json(path/'status.json',{})
    if old.get('state') in ('RUNNING','ORPHANED'):
        if old.get('state')=='RUNNING': event(path,'ORPHANED','máy tắt/giám sát mất')
        cli('journal.py','complete','--task-id',ident,'--attempt-id',job['attempt_id'],'--status','failed','--summary','máy tắt/giám sát mất',journal=job['journal'])
        if not next_attempt(path,job,'máy tắt/giám sát mất'): return
    while True:
        if task(state(job['queue'],job['journal']),ident)['status']=='accepted': return
        worker=job.get('current_worker',job['worker'])
        if _reporting().enabled() and _health().is_open(worker):
            chain=[job['worker']]+job['fallback_workers']
            position=chain.index(worker) if worker in chain else 0
            target=next((candidate for candidate in chain[position+1:]
                         if _policy().check(job,'FALLBACK',candidate)[0]
                         and not _health().is_open(candidate)),None)
            detail=f'worker {worker} tạm nghỉ 60 phút vì lỗi lặp'
            cli('journal.py','complete','--task-id',ident,'--attempt-id',job['attempt_id'],
                '--status','failed','--summary',detail,journal=job['journal'])
            log_job(job,worker,'BLOCKED',detail)
            if target and next_attempt(path,job,detail,worker=target):
                event(path,'CIRCUIT_FALLBACK',f'{worker} → {target}')
                continue
            status(path,state='BLOCKED',started_at=stamp(),attempt_id=job['attempt_id'],
                   reason=detail,error_type='ESCALATE',
                   error_signature='worker-unhealthy-'+worker)
            return
        decision=job_gate(job)
        brief_bytes=Path(job['brief']).read_bytes()
        if hashlib.sha256(brief_bytes).hexdigest()!=decision['binding']['brief_sha256']: raise delegation_gate.GateRefused('DELEGATION_BRIEF_SNAPSHOT_MISMATCH')
        brief_text=brief_bytes.decode('utf-8')
        if job.get('retry_note'):
            brief_text += '\n\n' + job['retry_note']
        worker_brief_bytes=brief_text.encode('utf-8')
        delegation_gate.claim(gate_journal(job),job['attempt_id'],worker,job['brief'])
        c=read_json(job['workers'],{})[worker]
        if not c.get('enabled'): raise RuntimeError(f'worker disabled: {worker}')
        key=key_value(c)
        if key: secrets.append(key)
        env=os.environ.copy(); env.update(c.get('env',{}))
        resolved, version, _ = resolve_worker_command(c['command'])
        command=[x.replace('{brief}',brief_text).replace('{key}',key or '') for x in resolved]
        log=path/'worker.log'
        log_start=log.stat().st_size
        pre_result_signatures=signatures(files(job))
        with log.open('ab') as output:
            output.write(f'\n=== {job["attempt_id"]} {worker} ===\n'.encode()); output.flush()
            stdin=subprocess.PIPE if c['brief_mode']=='stdin' else subprocess.DEVNULL
            job_gate(job, ('EXECUTING',))
            if _reporting().enabled():
                try:
                    counter_key, runs_total, effective_max = _counter().reserve(
                        job, worker, job.get('recovery_type', 'INITIAL'))
                except Exception as exc:
                    blocked_reason = f'Không thể chạy: {redact(str(exc))}'
                    log_job(job, worker, 'BLOCKED', blocked_reason)
                    try:
                        cli('journal.py','complete','--task-id',ident,'--attempt-id',job['attempt_id'],
                            '--status','failed','--summary',blocked_reason,journal=job['journal'])
                        cli('journal.py','checkpoint','--task-id',ident,'--by','ao-run',
                            '--next-action','Owner kiểm tra bộ đếm hoặc policy.lock',
                            '--blocked-reason',blocked_reason,journal=job['journal'])
                    except Exception as ledger_exc:
                        event(path,'LEDGER_ERROR',str(ledger_exc))
                    status(path, state='BLOCKED', reason=blocked_reason, error_type='ESCALATE',
                           error_signature='run-counter-or-policy')
                    return
                job['runs_total'] = runs_total
                job['counter_key'] = counter_key
                atomic_json(path/'job.json', job)
            log_job(job,worker,'RUNNING','trước khi chạy worker')
            p=subprocess.Popen(command,cwd=c['cwd'],env=env,stdin=stdin,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,**hidden_popen_kwargs(new_process_group=True))
            delegation_gate.attach_child(gate_journal(job),job['attempt_id'],worker,job['brief'],p.pid)
            def copy_output():
                for line in iter(p.stdout.readline,b''):
                    clean=line.decode('utf-8',errors='replace')
                    if key: clean=clean.replace(key,'[REDACTED]')
                    output.write(redact(clean).encode('utf-8')); output.flush()
            reader=threading.Thread(target=copy_output,daemon=True); reader.start()
            if stdin==subprocess.PIPE:
                p.stdin.write(worker_brief_bytes); p.stdin.close()
            event(path,'START',f'{job["attempt_id"]} worker={worker} pid={p.pid} exe={command[0]} version={version or "unknown"}')
            status(path,id=ident,state='RUNNING',attempt_id=job['attempt_id'],worker=worker,retries=job['retries'],supervisor_pid=os.getpid(),supervisor_created=process_start_time(os.getpid()),worker_pid=p.pid,worker_created=process_start_time(p.pid),last_log_change=stamp())
            watched=[log]+files(job); sig=signatures(watched)
            progress_sig=signatures(progress_files(job['brief']))
            last=time.monotonic(); started=last; last_workspace_scan=last
            # Leave room for filesystem timestamp rounding at process startup.
            workspace_since_ns=time.time_ns()-2_000_000_000
            workspace_root=Path(__file__).resolve().parents[2]
            scan_roots,scan_fallback=workspace_roots(job,workspace_root,
                lambda kind, detail: event(path,kind,detail))
            reason=None
            while p.poll() is None:
                if (path/'stop.json').exists():
                    reason='Owner/AO dừng'; kill_tree(p); break
                time.sleep(.25)
                fresh=signatures(watched)
                fresh_progress=signatures(progress_files(job['brief']))
                if fresh!=sig or fresh_progress!=progress_sig:
                    sig=fresh; progress_sig=fresh_progress
                    last=time.monotonic(); status(path,last_log_change=stamp())
                now=time.monotonic()
                if now-last_workspace_scan >= 5:
                    last_workspace_scan=now
                    changed_ns=recent_scoped_change(scan_roots,workspace_since_ns,scan_fallback)
                    if changed_ns:
                        workspace_since_ns=changed_ns
                        last=now; status(path,last_log_change=stamp())
                cause=stop_cause(last,time.monotonic(),started,c)
                if cause=='MAX_RUNTIME':
                    reason=stop_for_max_runtime(path,p,c['max_runtime_minutes']); break
                if cause=='STALL':
                    reason='treo: không có thay đổi log, kết quả, PROGRESS hoặc tệp dự án'; event(path,'STALL',reason); kill_tree(p); break
            code=p.wait()
            reader.join(timeout=5)
        delegation_gate.finish(gate_journal(job),job['attempt_id'],worker,job['brief'])
        # Never write key-bearing output back to any metadata file.
        with log.open('rb') as source:
            source.seek(log_start)
            tail=source.read().decode('utf-8',errors='replace')[-12000:]
        last_error=next((redact(line.strip()) for line in reversed(tail.splitlines()) if line.strip()),'')
        missing=failed_signals(job)
        if _reporting().enabled() and code == 0 and pre_result_signatures == signatures(files(job)):
            for signal in job['signals']:
                if signal.startswith('file:') and signal not in missing:
                    missing.append(signal)
        end_kind='STOPPED' if reason=='Owner/AO dừng' else 'STALLED' if reason and ('treo:' in reason or reason.startswith('MAX_RUNTIME:')) else 'DONE' if code==0 and not missing and not reason else 'BLOCKED'
        log_job(job,worker,end_kind,reason or last_error or f'exit={code}')
        if reason=='Owner/AO dừng':
            cli('journal.py','complete','--task-id',ident,'--attempt-id',job['attempt_id'],'--status','failed','--summary',reason,journal=job['journal'])
            event(path,'STOPPED',reason); status(path,state='STOPPED'); return
        if code==0 and not missing and not reason:
            summary=next((line.strip()[:180] for line in reversed(tail.splitlines()) if 'DONE' in line or 'tokens used' in line),'worker hoàn tất; tiêu chí đạt')
            cli('journal.py','complete','--task-id',ident,'--attempt-id',job['attempt_id'],'--status','done','--summary',redact(summary),journal=job['journal'])
            cli('journal.py','verify-task','--task-id',ident,journal=job['journal'])
            event(path,'DONE_NEEDS_REVIEW',job['attempt_id']); status(path,state='DONE_NEEDS_REVIEW'); return
        details=[f'exit={code}']
        if missing: details.append('tiêu chí chưa đạt: '+', '.join(missing))
        markers=c.get('done_markers',[])
        if markers and not any(x.lower() in tail.lower() for x in markers):
            details.append('thiếu dấu xong trong log: '+', '.join(markers))
        hint=pattern_hint(tail,c.get('error_patterns',[]))
        if hint: details.append(hint)
        reason='; '.join(([reason] if reason else [])+details)
        cli('journal.py','complete','--task-id',ident,'--attempt-id',job['attempt_id'],'--status','failed','--summary',reason,journal=job['journal'])
        if not _reporting().enabled():
            event(path,'FAILED',reason)
            if next_attempt(path,job,reason):
                continue
            status(path,state='GAVE_UP',reason=reason)
            return
        classification=_classifier().classify(worker,tail,code,missing,reason)
        try:
            health=_health().record(worker,classification['signature'])
            if health['opened']:
                event(path,'CIRCUIT_OPEN',f'{worker} nghỉ đến {health["open_until"]}')
                log_job(job,worker,'BLOCKED','WORKER_UNHEALTHY: tạm nghỉ 60 phút')
        except (OSError, ValueError):
            event(path,'HEALTH_LEDGER_ERROR','Không ghi được trạng thái worker')
        used=int(job.get('runs_total',1))-1 if _reporting().enabled() else int(job.get('retries',0))
        allowed=min(int(job.get('max_retries',0)),_policy().MAX_TOTAL_RUNS-1) if _reporting().enabled() else int(job.get('max_retries',0))
        action=_classifier().action(classification['type'],used,bool(job['fallback_workers']),allowed)
        approved = classification.get('approved_action')
        if approved == 'retry': action = 'RETRY'
        elif approved == 'fallback': action = 'FALLBACK' if job['fallback_workers'] else 'ESCALATE'
        elif approved == 'rework': action = 'RETRY'
        elif approved == 'needs_human': action = 'NEEDS_HUMAN'
        # ignore_benign cannot turn a failed exit or signal into a verified result.
        if _reporting().enabled() and int(job.get('runs_total',1)) >= _policy().MAX_TOTAL_RUNS and action in ('RETRY', 'FALLBACK'):
            action = 'ESCALATE'
        # Never retry a policy/approval denial, even if the retry budget remains.
        if action in ('RETRY','FALLBACK'):
            if action=='RETRY':
                target=worker
            else:
                chain=[job['worker']]+job['fallback_workers']
                position=chain.index(worker) if worker in chain else 0
                target=next((candidate for candidate in chain[position+1:] if candidate!=worker),None)
                if target is None:
                    action='ESCALATE'
                    reason += '; hết worker dự phòng chưa dùng'
        if action in ('RETRY','FALLBACK'):
            permitted, policy_detail = _policy().check(job, action, target)
            if not permitted:
                action = 'ESCALATE'
                reason += '; ' + policy_detail
        if action in ('RETRY','FALLBACK'):
            note=''
            if classification['type']=='FALSE_DONE':
                note='BẮT BUỘC ghi tệp kết quả '+', '.join(map(str,files(job)))+' khi xong.'
            elif classification['type']=='QC_FAIL':
                note='Lần trước trượt tiêu chí đo: '+reason
            log_job(job,worker,'BLOCKED','→'+action+'#'+str(used+1)+' ('+classification['type']+') '+reason)
            try:
                _reporting().recovery(job,classification,action,target,reason,policy_detail)
            except Exception:
                pass
            if classification['type']=='TRANSIENT':
                delays=os.environ.get('AO_REPORT_RETRY_DELAYS','30,120').split(',')
                time.sleep(float(delays[min(used,len(delays)-1)]))
            if next_attempt(path,job,reason,worker=target,retry_note=note):
                job['recovery_type'] = classification['type']
                atomic_json(path/'job.json', job)
                event(path,'AUTO_RECOVERY',action+' '+classification['type'])
                continue
        try:
            _reporting().recovery(job,classification,action,None,reason)
        except Exception:
            pass
        event(path,'FAILED',reason)
        status(path,state='BLOCKED',reason=last_error or reason,blocked=True,
               error_type='NEEDS_HUMAN' if action=='NEEDS_HUMAN' else 'ESCALATE',
               classified_type=classification['type'],error_signature=classification['signature'])
        cli('journal.py','checkpoint','--task-id',ident,'--by','ao-run',
            '--next-action','Owner kiểm tra và quyết định',
            '--blocked-reason',f'NEEDS_OWNER: {reason}',journal=job['journal'])
        return

def error_details(exc, secrets=()):
    def clean(value):
        for secret in secrets:
            if secret: value = value.replace(secret, '[REDACTED]')
        return redact(value)
    frames = traceback.extract_tb(exc.__traceback__)
    last = frames[-1] if frames else None
    return {'exception_type': type(exc).__name__, 'message': clean(str(exc)),
            'traceback_last': clean(f'{last.filename}:{last.lineno} in {last.name}: {last.line}') if last else '',
            'traceback': clean(''.join(traceback.format_exception(exc)))}


def report_error(path, exc, secrets=()):
    details = error_details(exc, secrets)
    job=read_json(path/'job.json',{})
    log_job(job,job.get('current_worker',job.get('worker','unknown')),'BLOCKED',details['message'])
    event(path, 'SUPERVISOR_ERROR', details['message'], error=details)
    status(path, state='SUPERVISOR_ERROR', error=details)


if __name__=='__main__':
    path=Path(sys.argv[1])
    secrets=[]
    try: run(path, secrets)
    except Exception as exc:
        report_error(path, exc, secrets)
        try:
            job=read_json(path/'job.json',{})
            if task(state(job['queue'],job['journal']),job['id'])['status']=='dispatched':
                cli('journal.py','complete','--task-id',job['id'],'--attempt-id',job['attempt_id'],
                    '--status','failed','--summary','supervisor error; xem events',journal=job['journal'])
            cli('journal.py','checkpoint','--task-id',job['id'],'--by','ao-run',
                '--next-action','Owner kiểm tra supervisor error',
                '--blocked-reason','NEEDS_OWNER: supervisor error',journal=job['journal'])
        except Exception as followup:
            event(path,'LEDGER_ERROR',str(followup))
