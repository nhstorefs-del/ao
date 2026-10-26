import argparse
import json
import os
from pathlib import Path
from common import ROOT, MEMORY, SESSIONS, DATA, matches, redact, ledger_state
import registry, ao_decide

SKIP={'node_modules','.git','__pycache__'}
def snippet(s,q,limit=240):
    from common import fold,compact
    clean=redact(s).replace('\n',' ')
    pos=fold(clean).find(fold(q))
    if pos<0:
        terms=fold(q).split()
        pos=next((fold(clean).find(t) for t in terms if t in fold(clean)),0)
    return clean[max(0,pos-60):max(0,pos-60)+limit]
def walk_names(root,depth):
    if not root.exists(): return
    for base,dirs,files in os.walk(root):
        level=len(Path(base).relative_to(root).parts)
        dirs[:]=[d for d in dirs if d not in SKIP and not d.startswith('.venv') and level<depth]
        for name in dirs+files: yield Path(base)/name
def message_text(obj):
    msg=obj.get('message',{})
    if not isinstance(msg,dict): return ''
    content=msg.get('content','')
    if isinstance(content,str): return content[:20000]
    if isinstance(content,list):
        return ' '.join(str(c.get('text','') or c.get('input','') if isinstance(c,dict) and c.get('type') in ('text','tool_use','tool_result') else '')[:4000] for c in content)[:20000]
    return ''
def search(query,sources=None,last=5,since=None,ledger=None,projects=None,decisions=None,memory=MEMORY,sessions=SESSIONS):
    sources=set(sources or ['ledger','registry','memory','decisions','transcripts'])
    out={s:[] for s in sources}
    def put(source,label,value,path='',ts='',score=0):
        if matches(query,value): out[source].append(dict(label=label,excerpt=snippet(value,query),path=str(path),ts=ts,score=score))
    if 'ledger' in sources:
        for t in (ledger if ledger is not None else ledger_state()).get('tasks',[]):
            put('ledger',t.get('id',''),f"{t.get('id','')} {t.get('title','')} {t.get('summary','')}",ROOT/'tools/ao-journal/queue.json',score=2)
    if 'registry' in sources:
        for p in (projects if projects is not None else registry.all_projects()):
            v=' '.join(str(p.get(k,'')) for k in ('id','title','folder','summary','next_step','key_files','waiting_on_owner'))
            put('registry',p['id'],v,p['folder'],score=3)
    if 'memory' in sources and Path(memory).exists():
        for path in Path(memory).rglob('*.md'):
            for n,line in enumerate(path.read_text(encoding='utf-8-sig',errors='replace').splitlines(),1):
                put('memory',path.name,line,f'{path}:{n}')
    if 'decisions' in sources:
        for d in (decisions if decisions is not None else ao_decide.records()):
            if not since or d['date']>=since: put('decisions',str(d['id']),d['quote']+' '+d['scope'],DATA/'decisions.jsonl',d['date'],2)
    if 'transcripts' in sources and Path(sessions).exists():
        for path in Path(sessions).glob('*.jsonl'):
            with path.open(encoding='utf-8',errors='replace') as f:
                for n,line in enumerate(f,1):
                    try: obj=json.loads(line)
                    except json.JSONDecodeError: continue
                    ts=obj.get('timestamp','')
                    if since and ts[:10]<since: continue
                    val=message_text(obj)
                    if val: put('transcripts',obj.get('type','message'),val,f'{path}:{n}',ts)
    if 'fs' in sources:
        for root,depth in [(ROOT,8)]+[(Path(p),5) for p in os.environ.get('AO_RECALL_EXTRA_ROOTS','').split(os.pathsep) if p]:
            for path in walk_names(root,depth): put('fs',path.name,path.name,path,score=1)
    for key,items in out.items():
        items.sort(key=lambda x:(x['score'],x['ts']),reverse=True)
        out[key]=items[:last]
    return out
def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(); p.add_argument('query'); p.add_argument('--in',dest='sources'); p.add_argument('--last',type=int,default=5); p.add_argument('--since'); p.add_argument('--rebuild',action='store_true'); p.add_argument('--json',action='store_true'); a=p.parse_args()
    out=search(a.query,a.sources.split(',') if a.sources else None,a.last,a.since)
    if a.json: print(json.dumps(out,ensure_ascii=False,indent=2)); return
    for source,items in out.items():
        print(f'## {source} ({len(items)})')
        for item in items: print(f"- {item['label']} · {item['ts']} · {item['path']}\n  {item['excerpt']}")
if __name__=='__main__': main()
