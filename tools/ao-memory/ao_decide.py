import argparse
import hashlib
import json
import re
from pathlib import Path
from common import DATA, MEMORY, locked, now, redact
import registry

PATH=DATA/'decisions.jsonl'
LINE=re.compile(r'^- (\d{4}-\d{2}-\d{2}) · [“"](.+?)[”"] · (.+)$')
def records(path=PATH):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()] if Path(path).exists() else []
def digest(row):
    return hashlib.sha256(json.dumps({k:v for k,v in row.items() if k!='hash'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def verify(path=PATH):
    prev='0'*64
    for row in records(path):
        if row['prev_hash']!=prev or row['hash']!=digest(row): return False
        prev=row['hash']
    return True
def add(quote,scope,project=None,task=None,official=False,date=None,path=PATH):
    with locked(path):
        rows=records(path)
        if rows and not verify(path): raise ValueError('Chuỗi hash không hợp lệ')
        row=dict(id=rows[-1]['id']+1 if rows else 1,ts=now(),date=date or now()[:10],
                 quote=redact(quote),scope=redact(scope),project=project,task=task,official=official,
                 prev_hash=rows[-1]['hash'] if rows else '0'*64)
        row['hash']=digest(row)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with Path(path).open('a',encoding='utf-8') as f:
            f.write(json.dumps(row,ensure_ascii=False)+'\n'); f.flush()
        return row
def import_approvals(source=MEMORY/'owner_approvals_log.md',path=PATH):
    imported=skipped=0
    for line in Path(source).read_text(encoding='utf-8-sig').splitlines():
        m=LINE.fullmatch(line.strip())
        if not m: skipped+=1; continue
        date,quote,scope=m.groups()
        if any(r['date']==date and r['quote']==quote and r['scope']==scope for r in records(path)):
            skipped+=1; continue
        add(quote,scope,official='APPROVAL BY OWNER' in quote.upper(),date=date,path=path)
        imported+=1
    return imported,skipped
def seed(path=PATH):
    """Không có quyết định mẫu; thêm bằng `ao_decide.py add`."""
    return None
def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='action',required=True)
    a=sub.add_parser('add'); a.add_argument('--quote',required=True); a.add_argument('--scope',required=True); a.add_argument('--project'); a.add_argument('--task'); a.add_argument('--official',choices=['yes','no'],default='no'); a.add_argument('--date')
    l=sub.add_parser('list'); l.add_argument('--project'); l.add_argument('--last',type=int); l.add_argument('--official',action='store_true')
    sub.add_parser('verify'); sub.add_parser('import-approvals')
    w=sub.add_parser('wait'); wsub=w.add_subparsers(dest='wait_action',required=True)
    wa=wsub.add_parser('add'); wa.add_argument('--project',required=True); wa.add_argument('--what',required=True)
    wd=wsub.add_parser('done'); wd.add_argument('--id',type=int,required=True)
    args=p.parse_args()
    if args.action=='add': out=add(args.quote,args.scope,args.project,args.task,args.official=='yes',args.date)
    elif args.action=='verify': out={'valid':verify()}
    elif args.action=='import-approvals': out=dict(zip(('imported','skipped'),import_approvals()))
    elif args.action=='list':
        out=[r for r in records() if (not args.project or r['project']==args.project) and (not args.official or r['official'])]
        if args.last: out=out[-args.last:]
    else:
        registry.seed()
        if args.wait_action=='add': out=registry.change(args.project,waiting_add=args.what)
        else:
            found=next((r for r in registry.all_projects() if any(x.get('id')==args.id for x in r['waiting_on_owner'])),None)
            if not found: raise KeyError(args.id)
            out=registry.change(found['id'],waiting_done=next(i+1 for i,x in enumerate(found['waiting_on_owner']) if x.get('id')==args.id))
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
