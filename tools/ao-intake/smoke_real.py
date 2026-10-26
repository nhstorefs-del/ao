"""Explicit one-shot manual ask proof. Never run as part of unittest."""
import json
import sys
from pathlib import Path
from store import Store,BASE
from dispatcher import Dispatcher

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    evidence=BASE/'evidence'; evidence.mkdir(exist_ok=True)
    output=evidence/'real_ask.json'
    if output.exists():
        raise SystemExit('Real proof already recorded; refusing another quota-consuming turn')
    store=Store()
    request=store.submit('chạy ao_status và nói dòng đầu','INTAKE2-real-ask-20261013','ask','manual-proof')
    if request['deduplicated']:
        raise SystemExit('Real request already exists; inspect its state, do not run again automatically')
    dispatcher=Dispatcher(store)
    token=dispatcher.claim(request['request_id'])
    if not token:
        raise SystemExit('Unable to claim real proof')
    final=dispatcher.execute(request['request_id'],token)
    result={'request_id':request['request_id'],'state':final['state'],
            'conversation':store.conversation(request['conversation_id']),
            'messages':store.messages(request['conversation_id']),
            'report_path':final['report_path'],'last_error':final['last_error']}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
