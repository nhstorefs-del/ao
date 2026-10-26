import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from common import HERE, ROOT, DATA, atomic_json, ledger_state, read_json, redact, JOURNAL, pid_alive
import registry, ao_decide
import ao_toolbox
import sys
sys.path.insert(0,str(HERE/'hub'))
import threads

HEADS=['CHỜ OWNER','ĐANG CHẠY','CẢNH BÁO','VỪA XONG 48 GIỜ','DỰ ÁN','QUYẾT ĐỊNH CHỐT GẦN NHẤT','NHỮNG ĐIỀU TUYỆT ĐỐI KHÔNG LÀM LẠI/KHÔNG QUÊN','TIN OWNER CHƯA XỬ LÝ/CẦN CLAUDE']
def age(ts):
    try: return (datetime.now(timezone.utc)-datetime.fromisoformat(ts.replace('Z','+00:00'))).total_seconds()/60
    except (ValueError,TypeError,AttributeError): return None
def generate(ledger=None, projects=None, decisions=None, rules=None, taskboard=None, events=None, brief=False):
    errors=[]
    def source(label,fn,fallback):
        try: return fn()
        except Exception as e: errors.append(f'không đọc được: {label} ({redact(str(e))[:120]})'); return fallback
    ledger=source('sổ việc',ledger_state,{}) if ledger is None else ledger
    projects=source('registry',lambda: registry.all_projects(),[]) if projects is None else projects
    decisions=source('decisions',ao_decide.records,[]) if decisions is None else decisions
    rules=source('data/rules.md',lambda:(DATA/'rules.md').read_text(encoding='utf-8'), '') if rules is None else rules
    def board():
        path=ROOT/'tools/taskboard/data/tasks.json'
        if not path.exists(): raise FileNotFoundError(path)
        return read_json(path,{})
    taskboard=source('taskboard/data/tasks.json',board,{}) if taskboard is None else taskboard
    events=source('events',lambda:list((HERE/'events').glob('*')) if (HERE/'events').exists() else [],[]) if events is None else events
    def receipt_times():
        found={}
        with (JOURNAL/'journal.jsonl').open(encoding='utf-8') as f:
            for line in f:
                r=json.loads(line)
                if r.get('task_id') and r.get('status') in ('accepted','dispatched','done','failed'):
                    found[r['task_id']]=r.get('ts')
        return found
    times=source('ao-journal/journal.jsonl',receipt_times,{}) if ledger and ledger.get('tasks') else {}
    groups={h:[] for h in HEADS}
    for p in projects:
        for w in p.get('waiting_on_owner',[]): groups[HEADS[0]].append(f"- {p['id']}: {w['what']} (từ {w['since']})")
        groups[HEADS[4]].append(f"- {p['id']} · {p['status']} · {p['folder']} · {p['next_step']}")
        if p['status']=='blocked': groups[HEADS[2]].append(f"- Dự án blocked: {p['id']}")
    for x in ledger.get('needs_owner',[]): groups[HEADS[0]].append('- '+redact(str(x))[:250])
    for x in ledger.get('workflow_checkpoints',[]):
        if str(x.get('blocked_reason','')).startswith('NEEDS_OWNER:'): groups[HEADS[0]].append(f"- {x.get('workflow')}: {x['blocked_reason']}")
    board_tasks=taskboard.get('tasks',[]) if isinstance(taskboard,dict) else taskboard if isinstance(taskboard,list) else []
    for t in board_tasks:
        for a in t.get('approvals',[]):
            if a.get('status')=='pending': groups[HEADS[0]].append(f"- Taskboard {t.get('id','')}: {a.get('title') or a.get('description') or a.get('what','approval pending')}")
    for t in ledger.get('tasks',[]):
        status=t.get('status'); tid=t.get('id'); mins=age(times.get(tid) or t.get('ts') or t.get('updated') or t.get('dispatched_at'))
        job=read_json(HERE/'jobs'/str(tid)/'status.json',{})
        if job:
            if status=='dispatched':
                running=job.get('state')=='RUNNING'
                alive=pid_alive(job.get('supervisor_pid'),job.get('supervisor_created')) if running else False
                display='MẤT_GIÁM_SÁT' if running and not alive else job.get('state','không rõ')
                groups[HEADS[1]].append(f"- Giám sát {tid}: {display} · retry {job.get('retries',0)} · log cuối {job.get('last_log_change','không rõ')}")
            if job.get('state') in ('GAVE_UP','ORPHANED','DONE_NEEDS_REVIEW','SUPERVISOR_ERROR') and not (brief and status=='accepted' and job.get('state')=='DONE_NEEDS_REVIEW'):
                groups[HEADS[2]].append(f"- {tid}: {job['state']} · retry {job.get('retries',0)}")
        if status=='dispatched':
            groups[HEADS[1]].append(f"- {tid}: {t.get('title')} · {round(mins)} phút" if mins is not None else f"- {tid}: {t.get('title')} · không rõ tuổi")
            if mins is not None and mins>60 and not job: groups[HEADS[2]].append(f'- Nghi treo: {tid} dispatched {round(mins)} phút')
        if status=='failed': groups[HEADS[2]].append(f'- Failed chưa retry: {tid} · {t.get("title")}')
        if status=='done' and (mins is None or mins>60): groups[HEADS[2]].append(f'- Done chưa accepted: {tid} · {t.get("title")}')
        if status=='accepted' and mins is not None and mins<=2880: groups[HEADS[3]].append(f'- {tid}: {t.get("title")}')
    for path in (HERE/'jobs').glob('*/status.json'):
        job=read_json(path,{})
        if job.get('state')!='RUNNING': continue
        tid=job.get('id') or path.parent.name
        if not pid_alive(job.get('supervisor_pid'),job.get('supervisor_created')):
            moment=job.get('last_log_change') or 'không rõ giờ (log cuối không có)'
            groups[HEADS[2]].append(f"- {tid}: giám sát mất từ {moment} (mốc log cuối) — chạy `ao_run.py resume --id {tid}` hoặc `reap --id {tid}`")
        else:
            log_mins=age(job.get('last_log_change'))
            if log_mins is not None and log_mins>60: groups[HEADS[2]].append(f'- Nghi treo: {tid} log không đổi {round(log_mins)} phút')
    for d in decisions[-(5 if brief else 10):][::-1]: groups[HEADS[5]].append(f"- {d['date']}: “{redact(d['quote'])[:150]}”")
    groups[HEADS[6]]=[x for x in rules.splitlines() if x.strip()][:15]
    if brief:
        return {'sections':groups,'errors':errors,'markdown':brief_markdown(groups,errors)}
    for message in source('threads',threads.pending,[]):
        groups[HEADS[7]].append(f"- {message['task']} · {message['ts']} · {redact(message['text'])[:120]}")
    output='# TRUNG TÂM CÔNG VIỆC\n\n'+'\n\n'.join('## '+h+'\n'+('\n'.join(groups[h]) or '- Không có') for h in HEADS)
    try:
        toolbox = ao_toolbox.markdown(ao_toolbox.load())
    except Exception as e:
        toolbox = 'không đọc được: toolbox (' + redact(str(e))[:120] + ')'
        errors.append(toolbox)
    output += '\n\n## (8) CÔNG CỤ CÓ SẴN — đọc trước khi nói "không có công cụ" hoặc nhờ Owner làm tay\n' + toolbox
    if errors: output+='\n\n## LỖI NGUỒN\n'+'\n'.join('- '+x for x in errors)
    return {'sections':groups,'errors':errors,'markdown':output}

def brief_markdown(groups, errors):
    """Keep SessionStart output below the hook's truncation threshold."""
    selected=[HEADS[i] for i in (0,1,2,3,5,6)]
    items={h:list(groups[h]) for h in selected}
    for h, limit in ((HEADS[2],10),(HEADS[3],8)):
        excess=max(0,len(items[h])-limit)
        items[h]=items[h][:limit] if h==HEADS[2] else items[h][-limit:][::-1]
        if excess: items[h].append(f'… và {excess} cảnh báo khác (chạy ao_state --stdout để xem)' if h==HEADS[2] else f'… và {excess} việc khác (chạy ao_state --stdout để xem)')
    # Reserve owner requests and running work; trim lower priority sections first.
    removed={h:0 for h in selected}
    intro=f'AO STATE ({datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")}) — đây là trạng thái tự nạp đầu phiên'
    reminder='Trước khi nói không có công cụ: `python tools\\ao-memory\\ao_toolbox.py find "<việc>"`; xem mục CÔNG CỤ CÓ SẴN trong `python tools\\ao-memory\\ao_state.py --stdout`.'
    def render():
        chunks=[intro]
        for h in selected:
            lines=items[h] or ['- Không có']
            if removed[h]: lines=lines+[f'… đã cắt {removed[h]} dòng; chạy ao_state --stdout để xem đủ']
            chunks.append('## '+h+'\n'+'\n'.join(lines))
        if errors: chunks.append('## LỖI NGUỒN\n'+'\n'.join('- '+x for x in errors))
        chunks.append(reminder)
        return '\n\n'.join(chunks)
    output=render()
    for h in reversed(selected):
        while len(output)>8000 and items[h]:
            items[h].pop(); removed[h]+=1; output=render()
    if len(output)>8000:
        # Exception messages are expendable; keep the source names visible.
        errors[:]=[x[:80]+'… đã cắt' if len(x)>80 else x for x in errors]
        output=render()
    return output
def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--stdout',action='store_true'); p.add_argument('--json',action='store_true'); p.add_argument('--brief',action='store_true'); a=p.parse_args()
    try:
        result=generate(brief=a.brief)
    except Exception as e:
        if a.brief:
            print('ao_state lỗi: '+redact(str(e))[:200]); return
        raise
    if a.out or not a.brief: Path(a.out or HERE/'STATE.md').write_text(result['markdown'],encoding='utf-8')
    if a.json: print(json.dumps(result,ensure_ascii=False,indent=2))
    elif a.stdout or a.brief: print(result['markdown'])
if __name__=='__main__': main()
