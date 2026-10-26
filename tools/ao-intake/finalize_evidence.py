"""Create bounded worker handoff from persisted artifacts, without any real retry."""
import ast
import hashlib
import json
import sys
from pathlib import Path
from datetime import datetime,timezone
from store import Store,BASE

ROOT=BASE.parents[1]

def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    evidence=BASE/'evidence'
    checks=json.loads((evidence/'verification.json').read_text(encoding='utf-8'))
    assert all(x['exit_code']==0 for x in checks if x['check']!='protected source hashes'),checks
    drift=next(x for x in checks if x['check']=='protected source hashes')
    independent=json.loads((evidence/'independent_results.json').read_text(encoding='utf-8'))
    assert independent['passed']==independent['total']==97
    real=json.loads((evidence/'real_ask.json').read_text(encoding='utf-8'))
    store=Store(); row=store.get(real['request_id'])
    assert row['state']==real['state']=='NEEDS_OWNER_ANSWER'
    assert store.messages(row['conversation_id'])==real['messages']
    assert store.conversation(row['conversation_id'])['claude_session_id']==real['conversation']['claude_session_id']
    assert Path(row['report_path']).read_text(encoding='utf-8')==real['messages'][-1]['text']
    with store.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0]==1
        events=[dict(x) for x in c.execute('SELECT * FROM events ORDER BY seq')]
    assert [e['state_to'] for e in events]==['RECORDED','CLAIMED','RUNNING','NEEDS_OWNER_ANSWER']
    assert len(list((ROOT/'tools'/'ao-memory'/'jobs').glob(row['request_id']+'*')))==0
    files=['store.py','processes.py','dispatcher.py','recover.py','test_intake.py','verify.py','smoke_real.py','finalize_evidence.py','README.md']
    for name in files:
        if name.endswith('.py'): ast.parse((BASE/name).read_text(encoding='utf-8'),filename=name)
    manifest={name:hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in files}
    intake=(evidence/'intake.txt').read_text(encoding='utf-8')
    assert 'Ran 39 tests' in intake and '\nOK' in intake
    state={'task':'INTAKE2_1013','at':datetime.now(timezone.utc).isoformat(),'status':'DONE_NEEDS_REVIEW',
           'scope':'tools/ao-intake excluding step1_test; result_INTAKE2.md',
           'outcome':'Durable SQLite intake, one-pass Claude dispatcher, multi-turn queue, sensitive gate, conservative recovery',
           'verification':{'offline_intake':39,'reporting':34,'independent':'97/97','immutable_manifest':'4 OK','repository_drift':drift,'db_integrity':'ok','real_turns':1},
           'real_request':real['request_id'],'real_state':real['state'],
           'artifacts':files+['data/intake.db','evidence/real_ask.json','evidence/verification.json'],
           'acceptance':'Worker tests passed; independent supervisor acceptance pending; no journal accept',
           'next_action':'Supervisor inspect artifacts/evidence and concurrent AO file drift; settle ask status-command conflict before any expanded permission',
           'limits':['No UI/scheduler/approval-token flow','Worker write boundary not machine-enforced; no full-tree per-job guard','scan is preview; --apply also required for no-job Claude recovery','real ask cannot execute ao_status by design']}
    (BASE/'STATE_INTAKE2.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    (evidence/'artifact_checks.json').write_text(json.dumps({'files_sha256':manifest,'events':events,'db_integrity':'ok','messages_and_report_equal':True,'real_ao_jobs':0},ensure_ascii=False,indent=2),encoding='utf-8')
    report=['# Kết quả INTAKE2 — 13/10/2026',
            'Trạng thái: DONE_NEEDS_REVIEW; đã triển khai và chạy test, chờ supervisor nghiệm thu độc lập. Không tự journal accept.',
            f'## Tệp tạo (gốc {ROOT / "tools" / "ao-intake"})',
            '- store.py, processes.py, dispatcher.py, recover.py, test_intake.py, README.md.',
            '- verify.py, smoke_real.py, finalize_evidence.py, STATE_INTAKE2.json; data/intake.db; evidence/*.json/*.txt; requests/'+real['request_id']+'/{prompt.md,worker_brief.md,report.md}.',
            '## Kết quả kiểm thử thật',
            '- `cd tools/ao-intake; python -m unittest test_intake`: Ran 39 tests, OK. Log: tools/ao-intake/evidence/intake.txt.',
            '- Reporting: đủ 7 module yêu cầu, Ran 34 tests, OK. Log: evidence/reporting.txt.',
            '- Bộ độc lập: TOTAL 97/97 PASS. Chạy nguyên run_independent.main(), chỉ chuyển OUTPUT trong bộ nhớ vào intake/evidence để không ghi ngoài phạm vi; không sửa mã suite/adapter. Log: evidence/independent.txt; JSON: independent_results.json.',
            '- `sha256sum -c independent_tests.SHA256`: DESIGNER_NOTE.md, FORMAT.md, ORACLE_RULES.md, scenarios.json: 4 OK. Log: evidence/hashes.txt.',
            '- Guard toàn kho chưa sạch: STATE.md đổi trong khi test; sau test phát hiện thêm haiku_worker.py và tests/test_haiku_worker.py trong ao-memory. Nguồn thay đổi đồng thời chưa xác minh; không sửa/hoàn tác. Lưu verification_with_live_state_drift.json + verification.json. step1_test/reporting/đề độc lập giữ nguyên.',
            '- SQLite integrity_check=ok; session/messages/report/events thật khớp; lưu evidence/artifact_checks.json. Supervisor cần đối chiếu thay đổi đồng thời trước nghiệm thu.',
            '## Một lượt Claude thật, không gửi gì ra ngoài',
            '- request_id: `'+real['request_id']+'`; trạng thái cuối `'+real['state']+'`; turn_count=1; session_id=`'+real['conversation']['claude_session_id']+'`.',
            '- Message Owner nguyên văn: '+real['messages'][0]['text'],
            '- Message Claude nguyên văn: '+real['messages'][1]['text'].replace('\n','<br>'),
            '- Chỉ 1 lượt thật; 0 job AO. Bằng chứng đầy đủ: tools/ao-intake/evidence/real_ask.json. Sau khi lưu thành công, lần in console đầu gặp UnicodeEncodeError cp1252; đã sửa stdout UTF-8, test CLI tiếng Việt xanh; không chạy lại Claude.',
            '## Hành vi và giới hạn',
            '- WAL+FULL, commit trước ack; idempotency; similar_to 24h chỉ cảnh báo; CAS và events chỉ-thêm; khóa hội thoại, queue reply, --resume. Hai lỗi cạnh tranh (child thoát trước commit; khóa sót sau terminal commit) có test riêng.',
            '- ask CHỈ Read (thêm --tools để thu hẹp thật), nên không thể chạy ao_status. Lượt thật xác nhận nối dây + chặn hành động; chưa chứng minh ao_status được thực thi dưới ask. Không nới quyền.',
            '- Quyền task đúng mẫu start/status/ao_status và 8 disallowedTools; ba biến hook=0; stdin DEVNULL; timeout 20 phút; max-turns 12/4. Máy ghi report từ câu trả lời cuối, không cấp Write/Edit cho Claude.',
            '- Cổng từ khóa chỉ lưới phụ, luôn WAITING_OWNER cho nhạy cảm; chưa nhận thẻ duyệt. Claude tự chọn làn/thợ, máy không chọn làn theo từ khóa.',
            '- recover scan chỉ đề xuất; --apply mới thực hiện (kể cả chưa có job). Job DONE/live/unknown giữ nguyên; job chết đề xuất resume, tự apply chỉ ORPHANED/STALLED. Có job không giao lại Claude; no-job tối đa 2 lần. DONE/FAILED/WAITING_OWNER không chạy lại.',
            '- Y5.2 kế hoạch rộng chưa đủ: chưa cưỡng chế tham số worker/workflow/phạm vi ghi bằng máy, chưa sandbox thợ/guard toàn cây sau job nền. Không coi hash regression là guard thợ. Chỉ việc nháp không tác dụng phụ ngoài máy.',
            '- DONE ở intake là hoàn tất lượt có report, không thay nghiệm thu AO; chưa tự đồng bộ DONE khi job nền xong, chưa retry quota/tự cấp duyệt, chưa UI/lịch. README có hướng dẫn và giới hạn đầy đủ.',
            '- Không sửa hub/ao_run/.claude/Windows lịch/luật chung; không bus, không Codex subagent/CLI, không đọc hoặc in khóa/token. Bộ nhớ bàn giao chỉ trong STATE_INTAKE2.json đúng phạm vi.']
    output=ROOT/'projects'/'AO_BRANCH_SPLIT_20261012'/'result_INTAKE2.md'
    formatted=[]
    for line in report:
        if line.startswith('#'):
            if formatted: formatted.append('')
            formatted.extend([line,''])
        else:
            formatted.append(line)
    output.write_text('\n'.join(formatted)+'\n',encoding='utf-8')
    print('Artifact checks OK; report:',output)

if __name__=='__main__': main()
