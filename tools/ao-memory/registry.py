import argparse
import json
from pathlib import Path
from common import DATA, ROOT, atomic_json, locked, now, read_json, ledger_state

PATH = DATA / 'projects.json'
SEEDS = [
 ('ao-core','AO journal và AMEND','tools/ao-journal','Sổ việc bền, điều phối viên độc lập và sửa spec có duyệt.','tools/ao-journal/RATIONALE.md','Kiểm tra các việc failed'),
 ('example','Dự án mẫu','docs','Ví dụ một dự án; thay bằng dự án thật của bạn.','README.md','Sửa registry cho dự án của bạn'),
]
WAITS = {'example':'Owner xác nhận danh sách dự án thật'}

def seed(path=PATH, state=None):
    with locked(path):
        if Path(path).exists():
            rows=read_json(path, [])
            changed=False; n=max((w.get('id',0) for r in rows for w in r.get('waiting_on_owner',[])),default=0)
            for r in rows:
                for w in r.get('waiting_on_owner',[]):
                    if 'id' not in w: n+=1; w['id']=n; changed=True
            if changed: atomic_json(path,rows)
            return rows
        state = state if state is not None else ledger_state()
        tasks = state.get('tasks', [])
        rows=[]
        for ident,title,folder,summary,key,next_step in SEEDS:
            folder = str(ROOT / folder) if not ':' in folder else str(Path(folder))
            key = str(ROOT / key)
            words = {'ao-core':['AMEND','T1','T2'], 'example':['EX']}.get(ident, [])
            ids=[t['id'] for t in tasks if any(t['id'].startswith(w) for w in words)]
            rows.append(dict(id=ident,title=title,folder=folder,status='active',summary=summary,
                key_files=[key],task_ids=ids,waiting_on_owner=[{'id':len(rows)+1,'what':WAITS[ident], 'since':'2026-10-02'}] if ident in WAITS else [],next_step=next_step,updated=now()))
        atomic_json(path,rows)
        return rows

def all_projects(path=PATH):
    return read_json(path, [])

def change(ident, path=PATH, **changes):
    with locked(path):
        rows=read_json(path,[])
        row=next((r for r in rows if r['id']==ident),None)
        if row is None: raise KeyError(ident)
        for k,v in changes.items():
            if k=='waiting_add':
                next_id=max((w.get('id',0) for r in rows for w in r['waiting_on_owner']),default=0)+1
                row['waiting_on_owner'].append({'id':next_id,'what':v,'since':now()[:10]})
            elif k=='waiting_done':
                n=int(v)-1
                if not 0 <= n < len(row['waiting_on_owner']): raise IndexError(v)
                row['waiting_on_owner'].pop(n)
            elif k in ('task','file'):
                field={'task':'task_ids','file':'key_files'}[k]
                if v not in row[field]: row[field].append(v)
            else: row[k]=v
        row['updated']=now(); atomic_json(path,rows)
        return row

def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['list','show','set']); p.add_argument('id',nargs='?')
    p.add_argument('--status',choices=['active','paused','done','blocked']); p.add_argument('--summary'); p.add_argument('--next'); p.add_argument('--waiting-add'); p.add_argument('--waiting-done',type=int); p.add_argument('--task'); p.add_argument('--file')
    a=p.parse_args(); seed()
    if a.action=='list': result=all_projects()
    elif a.action=='show': result=next(x for x in all_projects() if x['id']==a.id)
    else:
        updates={k:v for k,v in vars(a).items() if k not in ('action','id') and v is not None}
        if 'next' in updates: updates['next_step']=updates.pop('next')
        result=change(a.id,**updates)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
