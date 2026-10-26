"""Offline integration tests. No real Claude, AO dispatch or external send."""
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from store import Store
from dispatcher import Dispatcher, build_command, environment, DISALLOWED, sensitive, run_claude
from recover import scan
from processes import identity, alive

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = Store(self.root/'intake.db')
        self.calls=[]
        def fake(cmd,env,started):
            self.calls.append((cmd,env))
            started(999999999,'dead')
            return {'result':'Báo cáo mock đã kiểm.', 'session_id':'mock-session','is_error':False,'exit_code':0}
        self.d = Dispatcher(self.s,self.root/'requests',fake)

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self,text='Đối chiếu tài liệu',key='key',kind='task'):
        return self.s.submit(text,key,kind)

    def active(self,state='RUNNING'):
        r=self.submit(); token=self.d.claim(r['request_id'])
        if state=='RUNNING':
            self.s.cas(r['request_id'],'CLAIMED','RUNNING')
        with self.s.transaction() as c:
            c.execute('UPDATE requests SET run_pid=999999999,run_created=\'dead\'')
            c.execute('UPDATE conversations SET lock_pid=999999999,lock_created=\'dead\'')
        return self.s.get(r['request_id']),token

    def job(self,rid,suffix,state,**kw):
        folder=self.root/'jobs'/(rid+suffix); folder.mkdir(parents=True)
        (folder/'status.json').write_text(json.dumps(dict(state=state,**kw)),encoding='utf-8')

    def test_idempotency(self):
        r=self.submit(); again=self.submit()
        self.assertEqual(r['request_id'],again['request_id']); self.assertTrue(again['deduplicated'])
        self.assertEqual(len(self.s.list()),1)
        self.assertEqual(len(self.s.messages(r['conversation_id'])),1)

    def test_same_text_new_key_is_warning(self):
        a=self.submit(); b=self.submit(key='other')
        self.assertNotEqual(a['request_id'],b['request_id'])
        self.assertEqual(b['similar_to'],a['request_id']); self.assertEqual(len(self.s.list()),2)

    def test_similarity_expires_after_24h(self):
        a=self.submit()
        with self.s.transaction() as c:
            c.execute('UPDATE requests SET created_at=\'2000-01-01\' WHERE request_id=?',(a['request_id'],))
        self.assertIsNone(self.submit(key='other')['similar_to'])

    def test_ack_follows_commit_and_crash_rolls_back(self):
        def fail(c):
            with self.s.connect() as reader:
                self.assertEqual(reader.execute('SELECT count(*) FROM requests').fetchone()[0],0)
            raise RuntimeError('simulated crash before commit')
        with self.assertRaises(RuntimeError):
            self.s.submit('a','a',before_commit=fail)
        self.assertEqual(self.s.list(),[])
        with self.s.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],0)
        r=self.submit()
        self.assertIsNotNone(Store(self.s.path).get(r['request_id']))

    def test_actual_process_exit_before_commit(self):
        script='from store import Store; import os; s=Store('+repr(str(self.s.path))+'); s.submit("crash","crash",before_commit=lambda c: os._exit(19))'
        p=subprocess.run([sys.executable,'-B','-c',script],cwd=Path(__file__).parent,stdin=subprocess.DEVNULL,capture_output=True)
        self.assertEqual(p.returncode,19); self.assertEqual(self.s.list(),[])

    def test_wal_full_and_append_only(self):
        self.submit()
        with self.s.connect() as c:
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            self.assertEqual(c.execute('PRAGMA synchronous').fetchone()[0],2)
            with self.assertRaises(sqlite3.IntegrityError): c.execute('DELETE FROM events')
            with self.assertRaises(sqlite3.IntegrityError): c.execute('UPDATE events SET note=\'bad\'')

    def test_concurrent_submit_single_record(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            rows=list(pool.map(lambda _:self.submit(),range(2)))
        self.assertEqual(rows[0]['request_id'],rows[1]['request_id']); self.assertEqual(len(self.s.list()),1)

    def test_two_claimers_one_winner(self):
        r=self.submit(); barrier=threading.Barrier(2)
        def claim(_):
            barrier.wait(); return self.d.claim(r['request_id'])
        with concurrent.futures.ThreadPoolExecutor(2) as p: won=list(p.map(claim,range(2)))
        self.assertEqual(sum(bool(x) for x in won),1)

    def test_terminal_state_never_rewinds(self):
        r=self.submit(); self.d.once()
        self.assertFalse(self.s.cas(r['request_id'],'DONE','RECORDED'))
        with self.s.connect() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('UPDATE requests SET state=\'RECORDED\' WHERE request_id=?',(r['request_id'],))

    def test_sensitive_stops_without_runner(self):
        r=self.submit('Đăng bài lên Facebook'); self.d.once()
        self.assertEqual(self.s.get(r['request_id'])['state'],'WAITING_OWNER'); self.assertEqual(self.calls,[])

    def test_sensitive_groups(self):
        for text in ['hẹn bài', 'gửi tin nhắn','xóa dữ liệu','di chuyển thư mục','tải phần mềm','cài phần mềm','tốn tiền','sửa CLAUDE.md','edit AGENTS.md']:
            with self.subTest(text=text): self.assertTrue(sensitive(text))

    def test_sensitive_sens2_examples(self):
        texts = [
            'đẩy video lên kênh YouTube',
            'cho mình chuyển 500k cho nhà cung cấp',
            'đưa video lên youtube', 'up clip lên facebook',
            'tải lên fanpage', 'upload ảnh lên facebook', 'đăng tải video lên telegram',
            'đẩy ảnh lên youtube', 'đưa bài lên web', 'up tệp lên website',
            'tải báo cáo lên drive', 'upload dữ liệu lên cloud',
            'đăng clip', 'đăng ảnh', 'đăng hình', 'đăng bài viết',
            'đăng story', 'đăng status', 'đăng stt', 'livestream', 'phát trực tiếp',
            'chuyển tiền', 'chuyển khoản', 'chuyển vào tài khoản nhà cung cấp',
            'chuyển 500 k', 'chuyển 2tr', 'chuyển 2 triệu', 'chuyển 500 nghìn',
            'chuyển 500000đ', 'chuyển 500000 vnd', 'chuyển 20 usd',
            'chuyển 1,5 triệu', 'nạp tiền', 'rút tiền', 'trả lương', 'hoàn tiền',
            'thanh toán hóa đơn', 'đặt hàng', 'mua hàng', 'bán hàng',
            'mua sản phẩm', 'bán sản phẩm', 'mua cổ phiếu', 'bán cổ phiếu',
            'mua coin', 'bán coin', 'đặt giá', 'đổi giá', 'sửa giá', 'giảm giá',
            'chạy ads', 'quảng cáo sản phẩm', 'gửi báo cáo cho khách',
            'gửi bản nháp tới nhóm', 'gửi bảng thống kê đến người phụ trách',
            'gửi ảnh cho Lan', 'nhắn tin cho khách', 'gửi file ra ngoài',
            'gửi dữ liệu ra ngoài', 'chia sẻ công khai', 'tạo public link',
            'đặt công khai', 'xóa thư mục', 'gỡ ứng dụng', 'dọn ổ đĩa',
            'reset máy', 'format ổ đĩa', 'ghi đè tệp', 'overwrite file',
            'chmod 777 thư mục', 'chown user file', 'đổi tên hàng loạt',
            'sửa claude.md', 'xóa agents.md', 'thêm trong workers.json',
            'sửa settings.json', 'thêm vào .env', 'sửa khóa', 'thêm token',
            'đổi mật khẩu', 'sửa password', 'cập nhật api key',
            'ĐẨY VIDEO LÊN KÊNH YOUTUBE', 'day video len kenh youtube',
            'cho minh chuyen 500k cho nha cung cap',
            'đưa video\nlên kênh YouTube', 'gửi báo cáo\ncho khách',
            'thêm cấu hình\ntrong .env',
        ]
        self.assertGreaterEqual(len(texts), 25)
        for text in texts:
            with self.subTest(text=text):
                self.assertTrue(sensitive(text))

    def test_nonsensitive_sens2_examples(self):
        texts = [
            'tạo báo cáo tuần', 'tóm tắt tệp', 'đếm số dòng',
            'so sánh hai báo cáo', 'viết nháp kịch bản', 'tìm lỗi trong log',
            'đọc tài liệu', 'phân tích số liệu', 'liệt kê thư mục',
            'kiểm tra cú pháp Python', 'tạo bản nháp bài viết',
            'giải thích kết quả kiểm thử', 'tóm tắt báo cáo doanh thu',
            'chuyển văn bản thành chữ thường', 'chuyển bảng thành biểu đồ',
            'so sánh giá sản phẩm', 'đọc settings.json', 'tóm tắt AGENTS.md',
            'TẠO BÁO CÁO TUẦN', 'dem so dong',
        ]
        self.assertGreaterEqual(len(texts), 15)
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(sensitive(text))

    def test_sens2_regressions_wait_for_owner_without_runner(self):
        for i, text in enumerate([
            'đẩy video lên kênh YouTube',
            'cho mình chuyển 500k cho nhà cung cấp',
        ]):
            with self.subTest(text=text):
                r = self.submit(text, key=f'sens2-{i}')
                self.d.once()
                self.assertEqual(self.s.get(r['request_id'])['state'], 'WAITING_OWNER')
                self.assertEqual(self.calls, [])

    def test_ask_read_only(self):
        cmd=build_command('q','ask'); i=cmd.index('--allowedTools')
        self.assertEqual(cmd[i+1:cmd.index('--disallowedTools')],['Read','Glob','Grep'])
        self.assertNotIn('ao_run.py start',' '.join(cmd)); self.assertEqual(cmd[cmd.index('--max-turns')+1],'4')
        self.assertEqual(cmd[cmd.index('--tools')+1],'Read,Glob,Grep')

    def test_dispfix_commands_and_prompt(self):
        for kind, budget in [('task','0.40'),('ask','0.15')]:
            cmd = build_command('q',kind)
            self.assertEqual(cmd[cmd.index('--max-budget-usd')+1],budget)
            allowed = cmd[cmd.index('--allowedTools')+1:cmd.index('--disallowedTools')]
            for tool in ['Read','Glob','Grep']:
                self.assertIn(tool,allowed)
                self.assertIn(tool,cmd[cmd.index('--tools')+1].split(','))
            self.assertEqual(cmd[cmd.index('--disallowedTools')+1:],DISALLOWED)
            if kind == 'ask':
                self.assertNotIn('Bash',cmd[cmd.index('--tools')+1])
                self.assertFalse(any('ao_run' in tool for tool in allowed))
        r=self.submit(); self.d.once()
        prompt=(self.root/'requests'/r['request_id']/'prompt.md').read_text(encoding='utf-8')
        for expected in [str(self.root),'**/LANES/README*','projects/**','KHÔNG thử lại','CAN_OWNER_TRA_LOI:']:
            self.assertIn(expected,prompt)

    def diagnostic(self,rid):
        return json.loads((self.root/'requests'/rid/'run_result_1.json').read_text(encoding='utf-8'))

    def test_dispfix_max_turns_cost_and_event(self):
        r=self.submit()
        with self.s.transaction() as c:
            c.execute('UPDATE requests SET total_cost_usd=0.10 WHERE request_id=?',(r['request_id'],))
        self.d.runner=lambda *args:dict(is_error=True,subtype='error_max_turns',num_turns=13,total_cost_usd=0.29,result='')
        outcome=self.d.once()[0]
        self.assertEqual(outcome['state'],'FAILED')
        self.assertIn('Hết số bước cho phép (13/12)',outcome['last_error'])
        self.assertIn('hãy nói rõ đường dẫn rồi giao lại',outcome['last_error'])
        self.assertAlmostEqual(outcome['total_cost_usd'],0.39)
        self.assertEqual(self.diagnostic(r['request_id'])['total_cost_usd'],0.29)
        with self.s.connect() as c:
            note=c.execute('SELECT note FROM events WHERE request_id=? ORDER BY seq DESC LIMIT 1',(r['request_id'],)).fetchone()[0]
        self.assertEqual(note,outcome['last_error'])

    def test_dispfix_budget_failure(self):
        cases=[('task',{'is_error':True,'subtype':'error_max_budget_usd','total_cost_usd':0.39},'0.40'),
               ('ask',{'result':'incomplete','total_cost_usd':0.16},'0.15'),
               ('task',{'is_error':True,'result':'Maximum budget exceeded'},'0.40')]
        for i,(kind,data,budget) in enumerate(cases):
            self.submit(key=str(i),kind=kind);self.d.runner=lambda *args:data
            outcome=self.d.once()[0]
            self.assertEqual(outcome['state'],'FAILED')
            self.assertIn(f'Chạm trần chi phí cho việc này ({budget} USD)',outcome['last_error'])

    def test_dispfix_success_and_denials_sanitized(self):
        r=self.submit()
        command='find '+('x'*200)+' NEVER_PERSIST_TAIL'
        self.d.runner=lambda *args:dict(is_error=False,result='Budget analysis done '+('r'*600),total_cost_usd=0.01,
            permission_denials=[dict(tool_name='Bash',tool_input=dict(command=command,secret='NEVER_PERSIST_INPUT'),tool_use_id='PRIVATE_ID')],
            api_key='EXAMPLE_KEY',stop_reason='end_turn',terminal_reason='complete')
        outcome=self.d.once()[0]; self.assertEqual(outcome['state'],'DONE')
        self.assertIsNone(outcome['last_error']);self.assertAlmostEqual(outcome['total_cost_usd'],0.01)
        summary=self.diagnostic(r['request_id'])
        self.assertEqual(summary['permission_denials'],[dict(tool_name='Bash',command=command[:80])])
        self.assertEqual(len(summary['result']),500)
        self.assertNotIn('NEVER_PERSIST',json.dumps(summary));self.assertNotIn('PRIVATE_ID',json.dumps(summary))
        self.assertEqual(summary['stop_reason'],'end_turn')
        self.assertTrue((self.root/'requests'/r['request_id']/'report.md').is_file())

    def test_dispfix_denials_error_and_credentials(self):
        r=self.submit()
        self.d.runner=lambda *args:dict(is_error=True,subtype='error_max_turns',num_turns=13,
            result='api_key=TEST_SECRET Bearer TEST_BEARER',permission_denials=[dict(tool_name='Bash',tool_input=dict(command='token=TEST_TOKEN find files'))]*2)
        outcome=self.d.once()[0]
        self.assertIn('có 2 lệnh bị chặn vì ngoài quyền cho phép',outcome['last_error'])
        summary=json.dumps(self.diagnostic(r['request_id']))
        for secret in ['TEST_SECRET','TEST_BEARER','TEST_TOKEN']:
            self.assertNotIn(secret,summary)

    def test_dispfix_legacy_db_migration_preserves_rows(self):
        r=self.submit()
        with self.s.connect() as c:
            c.execute('ALTER TABLE requests DROP COLUMN total_cost_usd')
            before_events=c.execute('SELECT count(*) FROM events').fetchone()[0]
        reopened=Store(self.s.path)
        self.assertEqual(reopened.get(r['request_id'])['text'],r['text'])
        self.assertEqual(reopened.get(r['request_id'])['total_cost_usd'],0)
        self.assertEqual(Store(self.s.path).get(r['request_id'])['state'],'RECORDED')
        with reopened.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],before_events)
        self.assertEqual(len(reopened.messages(r['conversation_id'])),1)

    def test_dispfix_exception_diagnostic_no_raw_exception(self):
        r=self.submit()
        def fail(*args): raise RuntimeError('TEST_PRIVATE_EXCEPTION')
        self.d.runner=fail;outcome=self.d.once()[0]
        self.assertEqual(outcome['state'],'FAILED')
        summary=self.diagnostic(r['request_id'])
        self.assertEqual(summary['terminal_reason'],'RuntimeError')
        self.assertNotIn('TEST_PRIVATE_EXCEPTION',json.dumps(summary))

    def test_task_command_denials_hooks_resume(self):
        cmd=build_command('q','task','session')
        self.assertEqual(cmd[cmd.index('--disallowedTools')+1:cmd.index('--resume')],DISALLOWED)
        self.assertEqual(cmd[-2:],['--resume','session']); self.assertIn('Bash(python tools/ao-memory/ao_run.py start*)',cmd)
        self.assertEqual(cmd[cmd.index('--permission-mode')+1],'default')
        with patch.dict(os.environ,{'CLAUDE_SECRET':'bad','ANTHROPIC_API_KEY':'bad','claude_lower':'bad'}):
            env=environment(); self.assertNotIn('ANTHROPIC_API_KEY',env); self.assertNotIn('CLAUDE_SECRET',env)
            self.assertNotIn('claude_lower',env)
            for k in ['AO_ONOPEN_HOOK','AO_COVERAGE','AO_SESSIONSTART']: self.assertEqual(env[k],'0')

    def test_runner_stdin_and_timeout(self):
        with patch('dispatcher.subprocess.Popen') as popen:
            p=popen.return_value; p.pid=os.getpid(); p.returncode=0
            p.communicate.return_value=('{}'.strip(),'')
            run_claude(['fake'],{},lambda *args:None)
            self.assertEqual(popen.call_args.kwargs['stdin'],subprocess.DEVNULL)
            p.communicate.assert_called_once_with(timeout=1200)

    def test_done_report_messages_session(self):
        r=self.submit(kind='ask'); self.d.once(); row=self.s.get(r['request_id'])
        self.assertEqual(row['state'],'DONE'); self.assertTrue(Path(row['report_path']).is_file())
        self.assertEqual([m['role'] for m in self.s.messages(r['conversation_id'])],['owner','claude'])
        self.assertEqual(self.s.conversation(r['conversation_id'])['claude_session_id'],'mock-session')

    def test_question_then_reply_resumes(self):
        r=self.submit(kind='ask')
        self.d.runner=lambda *args:{'result':'CAN_OWNER_TRA_LOI: Chọn tài liệu nào?','session_id':'s1'}
        self.d.once(); self.assertEqual(self.s.get(r['request_id'])['state'],'NEEDS_OWNER_ANSWER')
        calls=[]
        self.d.runner=lambda cmd,*args: calls.append(cmd) or {'result':'Đã đọc','session_id':'s1'}
        reply=self.d.reply(r['conversation_id'],'Tài liệu A')
        self.assertEqual(reply['state'],'DONE'); self.assertEqual(calls[0][-2:],['--resume','s1'])
        self.assertEqual(self.s.get(r['request_id'])['state'],'NEEDS_OWNER_ANSWER')

    def test_busy_reply_queues_then_runs_once(self):
        r=self.submit(kind='ask'); token=self.d.claim(r['request_id'])
        reply=self.d.reply(r['conversation_id'],'Thông tin thêm','reply')
        self.assertTrue(reply['queued']); self.assertEqual(self.calls,[])
        self.d.execute(r['request_id'],token); self.d.once(); self.d.once()
        self.assertEqual(len(self.calls),2); self.assertEqual(self.calls[-1][0][-2:],['--resume','mock-session'])

    def test_conversation_different_requests_single_claim(self):
        r=self.submit(); b=self.s.submit('b','b',conversation_id=r['conversation_id'])
        self.assertIsNone(self.d.claim(b['request_id']))
        self.assertTrue(self.d.claim(r['request_id'])); self.assertIsNone(self.d.claim(b['request_id']))

    def test_dead_lock_after_terminal_commit_does_not_block_reply(self):
        r=self.submit(kind='ask'); self.d.once()
        with self.s.transaction() as c:
            c.execute('UPDATE conversations SET lock_token=\'crashed\',lock_pid=999999999,lock_created=\'dead\'')
        row=self.d.reply(r['conversation_id'],'Đọc tài liệu tiếp')
        self.assertEqual(row['state'],'DONE'); self.assertEqual(len(self.calls),2)
        self.assertEqual(self.s.get(r['request_id'])['state'],'DONE')

    def test_child_exit_does_not_unlock_live_dispatcher(self):
        r=self.submit()
        def runner(cmd,env,started):
            started(999999999,'dead')
            self.assertEqual(self.s.conversation(r['conversation_id'])['lock_pid'],os.getpid())
            self.assertEqual(scan(self.s,self.root/'jobs',True,self.d),[])
            return {'result':'ok','session_id':'s'}
        self.d.runner=runner; self.d.once()
        self.assertEqual(self.s.get(r['request_id'])['state'],'DONE')

    def test_quota_preserves_request(self):
        r=self.submit(); self.d.runner=lambda *args:{'is_error':True,'result':'OAuth session expired','session_id':'s'}
        self.d.once(); self.assertEqual(self.s.get(r['request_id'])['state'],'WAITING_QUOTA')
        self.assertEqual(self.s.get(r['request_id'])['text'],r['text'])

    def test_error_no_false_done(self):
        r=self.submit(); self.d.runner=lambda *args:{'is_error':True,'result':'failure'}
        self.d.once(); self.assertEqual(self.s.get(r['request_id'])['state'],'FAILED')

    def test_null_result_not_done(self):
        r=self.submit(); self.d.runner=lambda *args:{'result':None,'session_id':'s'}
        self.d.once(); self.assertEqual(self.s.get(r['request_id'])['state'],'FAILED')

    def test_quota_in_json_errors(self):
        r=self.submit(); self.d.runner=lambda *args:{'is_error':True,'errors':['You hit your limit'],'session_id':'s'}
        self.d.once(); self.assertEqual(self.s.get(r['request_id'])['state'],'WAITING_QUOTA')

    def test_pending_verification_not_done(self):
        r=self.submit(); self.d.runner=lambda *args:{'result':'CHO_NGHIEM_THU: worker đang chạy'}
        self.d.once(); self.assertEqual(self.s.get(r['request_id'])['state'],'WAITING_OWNER')

    def test_recover_done_jobs_untouched(self):
        r,_=self.active(); self.job(r['request_id'],'-1','DONE_NEEDS_REVIEW')
        actions=scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False)
        self.assertEqual(actions[0]['action'],'NO_TOUCH'); self.assertEqual(self.calls,[])

    def test_recover_dead_job_proposes_resume(self):
        r,_=self.active(); self.job(r['request_id'],'-1','ORPHANED')
        a=scan(self.s,self.root/'jobs',dispatcher=self.d,is_alive=lambda *x:False)
        self.assertEqual(a[0]['action'],'PROPOSE_RESUME'); self.assertIn('resume',a[0]['command']); self.assertEqual(self.calls,[])

    def test_recover_apply_only_stalled_or_orphaned(self):
        r,_=self.active(); self.job(r['request_id'],'-1','STALLED'); self.job(r['request_id'],'-2','FAILED')
        cmds=[]
        scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False,resume_runner=lambda c:cmds.append(c) or subprocess.CompletedProcess(c,0))
        self.assertEqual(len(cmds),1); self.assertEqual(cmds[0][-1],r['request_id']+'-1')

    def test_recover_live_supervisor_untouched(self):
        r,_=self.active(); self.job(r['request_id'],'-1','STALLED',supervisor_pid=3)
        a=scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda pid,*x:pid==3)
        self.assertEqual(a[0]['action'],'NO_TOUCH')

    def test_dead_running_job_proposed_but_not_applied(self):
        r,_=self.active(); self.job(r['request_id'],'-1','RUNNING')
        def forbidden(cmd): self.fail('RUNNING cannot auto resume')
        a=scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False,resume_runner=forbidden)
        self.assertEqual(a[0]['action'],'PROPOSE_RESUME'); self.assertFalse(a[0]['apply_eligible']); self.assertEqual(self.calls,[])

    def test_recover_terminal_request_no_run(self):
        self.submit(); self.d.once(); before=len(self.calls)
        self.assertEqual(scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False),[])
        self.assertEqual(len(self.calls),before)

    def test_recover_running_process_no_run(self):
        self.active(); self.assertEqual(scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:True),[])

    def test_recover_preview_does_not_mutate(self):
        r,_=self.active(); a=scan(self.s,self.root/'jobs',dispatcher=self.d,is_alive=lambda *x:False)
        self.assertEqual(a[0]['action'],'PROPOSE_CLAUDE_RECOVERY'); self.assertEqual(self.s.get(r['request_id'])['attempts'],0)

    def test_recover_max_two_actual_crashes(self):
        r,_=self.active()
        def crash(cmd,env,started):
            self.calls.append(cmd); started(999999999,'dead'); raise KeyboardInterrupt('simulated death')
        self.d.runner=crash
        for _ in range(2):
            with self.assertRaises(KeyboardInterrupt): scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False)
        a=scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False)
        self.assertEqual(a[0]['action'],'RETRY_LIMIT'); self.assertEqual(self.s.get(r['request_id'])['attempts'],2)
        self.assertEqual(len(self.calls),2)

    def test_recover_claimed_no_job(self):
        r,_=self.active('CLAIMED')
        scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False)
        self.assertEqual(self.s.get(r['request_id'])['state'],'DONE'); self.assertEqual(self.s.get(r['request_id'])['attempts'],1)
        self.assertIn('KHÔNG giao lại job đã có',self.calls[0][0][2])

    def test_corrupt_status_fail_closed(self):
        r,_=self.active(); self.job(r['request_id'],'-1','DONE')
        next((self.root/'jobs').glob('*/status.json')).write_text('{')
        a=scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False)
        self.assertEqual(a[0]['action'],'NO_TOUCH'); self.assertEqual(self.calls,[])

    def test_missing_status_existing_job_fail_closed(self):
        r,_=self.active(); (self.root/'jobs'/(r['request_id']+'-1')).mkdir(parents=True)
        a=scan(self.s,self.root/'jobs',True,self.d,is_alive=lambda *x:False)
        self.assertEqual(a[0]['action'],'NO_TOUCH'); self.assertEqual(self.calls,[])

    def test_cli_unicode_output_show_list(self):
        cmd=[sys.executable,'-B','store.py','--db',str(self.s.path)]
        p=subprocess.run(cmd+['submit','--text','Đọc yêu cầu tiếng Việt','--key','cli','--kind','ask'],cwd=Path(__file__).parent,stdin=subprocess.DEVNULL,capture_output=True)
        self.assertEqual(p.returncode,0); row=json.loads(p.stdout.decode('utf-8'))
        self.assertEqual(row['text'],'Đọc yêu cầu tiếng Việt')
        for args in [['show','--id',row['request_id']],['list','--state','RECORDED']]:
            p=subprocess.run(cmd+args,cwd=Path(__file__).parent,stdin=subprocess.DEVNULL,capture_output=True)
            self.assertEqual(p.returncode,0); self.assertTrue(json.loads(p.stdout.decode('utf-8')))

    def test_pid_reuse_and_current_process(self):
        created=identity(os.getpid()); self.assertTrue(alive(os.getpid(),created))
        self.assertFalse(alive(os.getpid(),'different')); self.assertFalse(alive(999999999,'dead'))

if __name__=='__main__':
    unittest.main()
