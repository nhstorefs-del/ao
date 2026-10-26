"""Drive the production recovery/reporting code in a fake-clock sandbox.

The scenarios are frozen and never modified. The adapter runs the real
production modules (classifier, policy, counter, outboxes, watchdog, digest,
checker, status) against a temporary sandbox root with a fake clock and a
stub Telegram sender. Any observation that cannot be measured is reported in
``unsupported_checks`` so the runner can never count an unmeasured case as
PASS.
"""
import importlib
import inspect
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ao_status
import check_the_checker
import counter_admin
import digest as digest_module
import error_classifier
import heartbeat
import human_outbox
import learn
import metrics
import normal_outbox
import recovery_policy
import reporting
import run_counter
import worker_health

WORKERS = {
    'codex-main': {'max_runtime_minutes': 60, 'error_patterns': []},
    'codex-2': {'max_runtime_minutes': 60, 'error_patterns': []},
    'deepseek-paid': {'max_runtime_minutes': 60, 'error_patterns': []},
    'deepseek-nvidia': {'max_runtime_minutes': 60, 'error_patterns': []},
    'gemini': {'max_runtime_minutes': 60, 'error_patterns': []},
    'selftest-bad': {'max_runtime_minutes': 60, 'error_patterns': []},
}
LAUNCHED = object()


class FakeDateTime(datetime):
    engine = None

    @classmethod
    def now(cls, tz=None):
        moment = cls.engine.time
        return moment if tz is None else moment.astimezone(tz)


def _bind_defaults(func, **overrides):
    names = [p.name for p in inspect.signature(func).parameters.values()
             if p.default is not inspect.Parameter.empty]
    current = dict(zip(names, func.__defaults__ or ()))
    current.update(overrides)
    func.__defaults__ = tuple(current[name] for name in names)


def run_scenario(scenario):
    with tempfile.TemporaryDirectory(prefix='ao-independent-') as temporary:
        return Engine(scenario, Path(temporary)).run()


class Engine:
    def __init__(self, case, root):
        self.case = case
        self.root = Path(root)
        self.setup = case.get('setup', {})
        self.zero = datetime.fromisoformat(self.setup['now'])
        self.time = self.zero
        self.saved = {}
        self.messages = []
        self.state = {}
        self.errors = []
        self.unsupported = []
        self.actions = []
        self.human_buffer = []
        self.human_worker = None
        self.breaker_pending = {}
        self.retry_briefs = {}
        self.retry_delays = {}
        self.invocations = {}
        self.pre_spawn = {}
        self.latency = {}
        self.outbound_hosts = []
        self.coordinator_calls = []
        self.original_briefs = {}
        self.signatures = {}
        self.type_counts = {}
        self.crash = None
        self.machine_off = False
        self.human_seen = {}
        self.urgent_notes = []
        self.telegram_times = []
        self.digest_texts = []
        self.ao_status_outputs = []
        self.jobs = {item['id']: item for item in self.setup.get('jobs', [])}
        self.active = []
        self.finished = set()
        self.log_file = self.root / 'tools' / 'ao-report' / 'JOBS.log'
        self._layout()
        self._apply_env()
        self.disabled = not reporting.enabled()
        FakeDateTime.engine = self
        self._patch(FakeDateTime)
        self._patch_modules()
        self._apply_policy_override()
        self._preload()
        for item in self.jobs.values():
            self._prepare(item)

    # ----- sandbox layout + module redirection -------------------------------
    def _layout(self):
        self.ar = self.root / 'tools' / 'ao-report'
        self.jobs_dir = self.root / 'tools' / 'ao-memory' / 'jobs'
        (self.ar / 'state').mkdir(parents=True, exist_ok=True)
        (self.ar / 'outbox').mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.sent = self.ar / 'sent.jsonl'
        self.job_log = self.ar / 'JOBS.log'
        self.job_pending = self.ar / 'JOBS.pending.jsonl'
        self.recovery = self.ar / 'recovery.jsonl'
        self.unknown = self.ar / 'error_patterns_unknown.jsonl'
        self.human = self.ar / 'outbox' / 'human.jsonl'
        self.normal = self.ar / 'outbox' / 'normal.jsonl'
        self.needs = self.ar / 'needs_human.md'
        self.counter = self.ar / 'state' / 'run_counter.json'
        self.health = self.ar / 'state' / 'worker_health.json'
        self.heart = self.ar / 'heartbeat.json'
        self.digest_log = self.ar / 'digest.jsonl'
        self.audit = self.ar / 'learned_audit.jsonl'
        self.patterns = self.ar / 'error_patterns.json'
        self.policy = self.ar / 'recovery_policy.json'
        self.lock = self.ar / 'policy.lock'
        self.reminders = self.ar / 'reminders.jsonl'
        self.legacy_file = self.ar / 'legacy.json'
        self.checker_state = self.ar / 'checker_state.json'
        self.patterns.write_text(error_classifier.PATTERNS.read_text(encoding='utf-8'),
                                 encoding='utf-8')
        shutil.copyfile(recovery_policy.POLICY, self.policy)
        shutil.copyfile(recovery_policy.LOCK, self.lock)

    def _apply_env(self):
        self.saved_env = dict(os.environ)
        os.environ['AO_AUTOREPORT'] = str(self.setup.get('env', {}).get('AO_AUTOREPORT', '1'))
        os.environ.pop('AO_REPORT_TEST_OUTBOX', None)

    def _patch(self, fakedt):
        for name in ('reporting', 'error_classifier', 'learn', 'run_counter',
                     'worker_health', 'counter_admin', 'check_the_checker',
                     'metrics', 'ao_status', 'heartbeat'):
            module = importlib.import_module(name)
            if hasattr(module, 'datetime'):
                self._set(module, 'datetime', fakedt)

    def _set(self, obj, name, value):
        key = (id(obj), name)
        if key not in self.saved:
            self.saved[key] = (obj, name, getattr(obj, name, None))
        setattr(obj, name, value)

    def _patch_modules(self):
        setter = self._set
        setter(reporting, 'HERE', self.ar)
        setter(reporting, 'JOBS', self.jobs_dir)
        setter(reporting, 'SENT', self.sent)
        setter(reporting, 'JOB_LOG', self.job_log)
        setter(reporting, 'JOB_LOG_PENDING', self.job_pending)
        setter(reporting, 'RECOVERY', self.recovery)
        setter(reporting, 'UNKNOWN', self.unknown)
        setter(reporting, 'now', self._clock)
        setter(human_outbox, 'OUTBOX', self.human)
        setter(human_outbox, 'NEEDS', self.needs)
        setter(normal_outbox, 'OUTBOX', self.normal)
        setter(normal_outbox, 'NOISE', self.ar / 'noise.jsonl')
        setter(run_counter, 'STATE', self.counter)
        setter(worker_health, 'STATE', self.health)
        setter(heartbeat, 'PATH', self.heart)
        setter(digest_module, 'LOG', self.digest_log)
        setter(digest_module, 'scheduled_status', None)
        setter(digest_module, 'coverage_lib', None)
        setter(learn, 'AUDIT', self.audit)
        setter(error_classifier, 'PATTERNS', self.patterns)
        setter(check_the_checker, 'HEART', self.heart)
        setter(check_the_checker, 'STATE', self.checker_state)
        setter(check_the_checker, 'HERE', self.ar)
        setter(recovery_policy, 'POLICY', self.policy)
        setter(recovery_policy, 'LOCK', self.lock)
        sender = self._send
        self._bind(reporting.pending, jobs=self.jobs_dir, sent=self.sent, clock=self._clock)
        self._bind(reporting.job_event, log=self.job_log, pending=self.job_pending)
        self._bind(reporting.repair_job_log, log=self.job_log, pending=self.job_pending)
        self._bind(reporting.latest, sent=self.sent)
        self._bind(reporting.notify, sender=sender, sent=self.sent, clock=self._clock)
        self._bind(human_outbox.enqueue, sender=sender, clock=self._clock,
                       outbox=self.human, needs=self.needs)
        self._bind(human_outbox.deliver, sender=sender, clock=self._clock, outbox=self.human)
        self._bind(human_outbox.pending_count, path=self.human)
        self._bind(human_outbox.signature_counts, path=self.human)
        self._bind(normal_outbox.enqueue, clock=self._clock, outbox=self.normal, launch=False)
        self._bind(normal_outbox.flush, sender=sender, clock=self._clock,
                       outbox=self.normal, sent=self.sent)
        self._bind(normal_outbox.latest, path=self.normal)
        self._bind(digest_module.run, sender=sender, clock=self._clock, log=self.digest_log)
        self._bind(heartbeat.beat, clock=self._clock, path=self.heart)
        self._bind(run_counter.lookup, path=self.counter)
        self._bind(run_counter.reserve, path=self.counter)
        self._bind(worker_health.record, path=self.health)
        self._bind(worker_health.is_open, path=self.health)
        self._bind(worker_health.read, path=self.health)
        self._bind(ao_status.collect, jobs=self.jobs_dir, clock=self._clock)
        self._bind(metrics.calculate, jobs=self.jobs_dir, sent=self.sent, clock=self._clock)
        self._bind(counter_admin.reset, path=self.counter)
        self._bind(learn.resolve, patterns=self.patterns, unknown=self.unknown)
        self._bind(learn.change, patterns=self.patterns)
        self._bind(error_classifier.classify, patterns=self.patterns)

    def _apply_policy_override(self):
        override = self.setup.get('policy')
        if override:
            data = json.loads(self.policy.read_text(encoding='utf-8'))
            data.update(override)
            self.policy.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            self._refresh_lock()

    def _refresh_lock(self):
        record = {'sha256': recovery_policy.policy_digest(),
                  'approved_by_owner_quote': 'adapter policy override',
                  'at': self.time.isoformat()}
        self.lock.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')

    def _bind(self, func, **overrides):
        key = (id(func), '__defaults__')
        if key not in self.saved:
            self.saved[key] = (func, '__defaults__', func.__defaults__)
        _bind_defaults(func, **overrides)

    def _restore(self):
        for key, (obj, name, value) in self.saved.items():
            setattr(obj, name, value)
        self.saved.clear()
        os.environ.clear()
        os.environ.update(self.saved_env)

    # ----- time + sending ----------------------------------------------------
    def _clock(self):
        return self.time

    def _minute(self):
        return (self.time - self.zero).total_seconds() / 60

    def _telegram_mode(self):
        mode = self.setup.get('telegram', {}).get('mode', 'ok')
        windows = self.setup.get('telegram', {}).get('windows', [])
        if mode == '429' and windows:
            mode = 'ok'
        for window in windows:
            if window['from_min'] <= self._minute() < window['to_min']:
                mode = window['mode']
        return mode

    def _send(self, text):
        text = str(text)
        lane = 'human' if text.startswith(('[VIỆC CẦN BẠN QUYẾT', '[KHẨN', '[GIÁM SÁT',
                                           '[BÃO LỖI]', '[THỬ NGHIỆM] [VIỆC CẦN')) else 'normal'
        mode = self._telegram_mode()
        if mode == 'ok':
            code = 0
        elif mode == '429':
            code = 429
        else:
            code = 99
        self.telegram_times.append(self._minute())
        if code == 0:
            self.messages.append({'t_min': self._minute(), 'lane': lane, 'text': text,
                                  'job_ids': self._ids_in(text),
                                  'escalation_id': self._escalation_for(text) if lane == 'human' else None,
                                  'delivered': True, 'attachment': False})
            if lane == 'normal':
                self.digest_texts.append(text)
        return code

    def _ids_in(self, text):
        return [ident for ident in self.jobs if re.search(r'\(%s\)' % re.escape(ident), text)]

    def _escalation_for(self, text):
        latest = {}
        for row in reporting.rows(self.human):
            latest[row['escalation_id']] = row
        for row in latest.values():
            if row.get('body') == text:
                return row['escalation_id']
        return None

    # ----- preload -----------------------------------------------------------
    def _preload(self):
        pre = self.setup.get('preload', {})
        groups = {}
        for key, row in pre.get('run_counter', {}).items():
            ident = key.split(':')[1] if key.startswith('ID:') else key
            groups[key] = {'runs_total': int(row.get('runs_total', 0)),
                           'runs': list(row.get('runs', [])), 'ids': [ident],
                           'brief_signal_hash': None, 'last_at': self.time.isoformat()}
        if groups:
            run_counter._save(self.counter, {'groups': groups})
        learned = {}
        for item in pre.get('learned', []):
            created = self.time - timedelta(days=float(item.get('created_days_ago', 0)))
            learned[item['signature']] = {
                'id': item.get('id', item['signature']), 'type': learn.SAFE.get(item['action'], 'UNKNOWN'),
                'action': item['action'], 'auto': bool(item.get('auto', True)),
                'state': item.get('state', 'PENDING_CONFIRMATION'),
                'status': item.get('state', 'PENDING_CONFIRMATION'),
                'applied_count': int(item.get('applied_count', 0)),
                'approved_by': 'owner', 'approved_by_owner_quote': item.get('quote', ''),
                'approved_at': created.isoformat(), 'sample': '', 'note': item.get('note', '')}
        if learned:
            data = json.loads(self.patterns.read_text(encoding='utf-8'))
            data['learned'] = learned
            self.patterns.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        for row in pre.get('human_outbox', []):
            created = self.time - timedelta(minutes=float(row.get('age_min', 0)))
            item = {'escalation_id': row.get('escalation_id', 'ESC-?'), 'id': row.get('id', '?'),
                    'attempt': row.get('attempt', 'x#1'), 'job_path': str(self.ar / 'jobs' / row.get('id', '?')),
                    'signature': row.get('signature', 'unknown'), 'lane': 'human',
                    'body': row.get('body', '[VIỆC CẦN BẠN QUYẾT] tiền tồn'),
                    'created_at': created.isoformat(),
                    'status': 'delivered' if row.get('delivered') else 'pending',
                    'tries': row.get('tries', 0), 'next_retry': created.isoformat()}
            reporting.append(self.human, item, durable=True)
        if pre.get('normal_msgs_sent_last_hour'):
            for index in range(int(pre['normal_msgs_sent_last_hour'])):
                reporting.append(self.sent, {'id': 'seed%d' % index, 'attempt': 'seed#1',
                                             'at': (self.time - timedelta(minutes=30)).isoformat(),
                                             'ok': True, 'status': 'sent', 'result_code': 0,
                                             'batch_sha256': 'seed%d' % index}, durable=True)
        for signature, minutes in pre.get('signature_seen_minutes_ago', {}).items():
            reporting.append(self.sent, {'id': 'sigseed', 'attempt': 'seed#1',
                                         'at': (self.time - timedelta(minutes=float(minutes))).isoformat(),
                                         'ok': True, 'status': 'sent', 'signature': signature}, durable=True)
        for signature, count in pre.get('signature_counts_7d', {}).items():
            for index in range(int(count)):
                reporting.append(self.unknown, {'at': (self.time - timedelta(days=1 + index)).isoformat(),
                                                'signature': signature, 'worker': 'seed',
                                                'reason': 'seed weekly'}, durable=True)
        for entry in pre.get('learned', []):
            pass
        if 'cost_7d_avg_per_day' in pre or 'auto_recovery_cost_today' in pre:
            self.cost_preload = pre
        hb = self.setup.get('heartbeat', {})
        data = {}
        for key, minutes in (('watchdog_last_beat_min_ago', 'watchdog'),
                             ('digest_last_beat_min_ago', 'digest')):
            if key in hb:
                data[minutes] = {'at': (self.time - timedelta(minutes=float(hb[key]))).isoformat(),
                                 'version': '2', 'watched': 1}
        if data:
            self.heart.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        if hb.get('jobs_log_last_write_hours_ago') is not None:
            stamp = self.time - timedelta(hours=float(hb['jobs_log_last_write_hours_ago']))
            self.job_log.write_text(f'{stamp.isoformat()} OLD old#1 worker RUNNING cũ\n'
                                    f'{stamp.isoformat()} OLD old#1 worker DONE cũ\n', encoding='utf-8')
        for name in self.setup.get('fs', {}).get('corrupt_files', []):
            if name.endswith('run_counter.json'):
                self.counter.write_text('{ this is not json', encoding='utf-8')
        pre_cost = self.setup.get('preload', {})
        if 'auto_recovery_cost_today' in pre_cost or 'cost_7d_avg_per_day' in pre_cost:
            today = pre_cost.get('auto_recovery_cost_today', 0)
            if today:
                reporting.append(self.recovery, {'at': self.time.isoformat(), 'id': 'seed',
                                                 'estimated_cost_usd': today}, durable=True)
            average = pre_cost.get('cost_7d_avg_per_day', 0)
            for days_ago in range(1, 8):
                if average:
                    reporting.append(self.recovery, {'at': (self.time - timedelta(days=days_ago)).isoformat(),
                                                     'id': 'seed', 'estimated_cost_usd': average}, durable=True)

    # ----- jobs --------------------------------------------------------------
    def _prepare(self, item):
        ident = item['id']
        path = self.jobs_dir / ident
        path.mkdir(parents=True, exist_ok=True)
        brief = path / 'brief.md'
        brief.write_text(item.get('brief_hash', ident), encoding='utf-8')
        self.original_briefs[ident] = brief.read_bytes()
        signal = self.root / 'tools' / 'ao-report' / item.get('signal_file', f'out/{ident}.json')
        signal.parent.mkdir(parents=True, exist_ok=True)
        job = {'id': ident, 'title': item.get('title', ident), 'brief': str(brief),
               'signals': ['file:' + str(signal)], 'attempt_id': ident + '#1',
               'worker': item.get('worker', 'deepseek-paid'),
               'fallback_workers': list(item.get('fallback_chain') or
                                        ([item['fallback_worker']] if item.get('fallback_worker') else [])),
               'max_retries': int(item.get('brief_args', {}).get('max_retries', 2)),
               'work_type': {'code_hard': 'code'}.get(item.get('kind'), 'simple'),
               'data_class': item.get('data_class', 'internal'),
               'kind': item.get('kind', 'read_write_simple'),
               'requires_owner_approval': item.get('kind') in {'publish', 'delete', 'spend'},
               'journal': str(self.root / 'tools' / 'ao-journal' / 'journal.jsonl')}
        (path / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        (path / 'status.json').write_text('{}', encoding='utf-8')
        self.state[ident] = {'status': 'NEW', 'attempts': 0, 'runs_total': 0,
                             'owner_notified': False, 'final_worker': job['worker'],
                             'fallback_switches': 0, 'last_qc': None}

    def _status(self, path, job, **data):
        current = reporting.read(path / 'status.json')
        current.update({'id': job['id'], 'attempt_id': job['attempt_id'], **data})
        (path / 'status.json').write_text(json.dumps(current, ensure_ascii=False), encoding='utf-8')

    def _start(self, ident):
        if ident in self.jobs and ident not in self.finished and ident not in self.active:
            self.active.append(ident)

    def _outcome(self, step):
        if isinstance(step, dict):
            return step.get('outcome', 'ok'), step.get('measure', ''), step.get('result', {})
        if step == 'hang':
            return 'hang', '', {}
        if ':' in step:
            kind, message = step.split(':', 1)
            return kind, message, {}
        return step, '', {}

    def run_job(self, ident):
        item = self.jobs[ident]
        path = self.jobs_dir / ident
        job = reporting.read(path / 'job.json')
        worker = job['worker']
        script = list(item.get('script', []))
        state = self.state[ident]
        delay_index = 0
        if self.disabled:
            self._run_disabled(ident, job, path, state)
            return
        health = worker_health.read()
        if worker_health.is_open(worker, self.time, health):
            for cand in job['fallback_workers']:
                if cand == worker:
                    continue
                allowed, _ = recovery_policy.check(job, 'FALLBACK', cand, policy_path=self.policy)
                if allowed:
                    self.actions.append('ROUTE-AWAY ' + worker + '->' + cand)
                    worker = cand
                    state['fallback_switches'] += 1
                    break
        for index, step in enumerate(script):
            job['attempt_id'] = f'{ident}#{index + 1}'
            with (path / 'events.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps({'ts': self.time.isoformat(), 'type': 'START',
                                       'detail': job['attempt_id']}) + '\n')
            try:
                key, total, limit = run_counter.reserve(job, worker,
                                                        'INITIAL' if index == 0 else 'AUTO_RECOVERY')
            except Exception as exc:
                message = str(exc)
                try:
                    existing = run_counter.lookup(job)[1]['runs_total']
                except Exception:
                    existing = 0
                if 'chạm trần' in message:
                    self.state[ident]['runs_total'] = max(existing, int(
                        (self.state[ident].get('runs_total') or 0)))
                    self._status(path, job, state='BLOCKED', error_type='ESCALATE',
                                 reason=message, error_signature='run-cap',
                                 runs_display=f'đã chạy {self.state[ident]["runs_total"]}/3',
                                 finished_at=self.time.isoformat())
                    self._escalate(ident, job, 'ESCALATE', message, 'run-cap',
                                   runs=self.state[ident]['runs_total'])
                else:
                    reason = message if 'policy.lock' in message or 'chính sách' in message \
                        else ('bộ đếm lần chạy hỏng hoặc khóa: ' + message)
                    self._start_refused(ident, job, reason)
                return
            if self.crash == 'after_counter_increment_before_spawn' and index == 0:
                self.pre_spawn[ident] = total
                self.state[ident]['runs_total'] = total
                self._status(path, job, state='RUNNING', worker=worker,
                             started_at=self.time.isoformat())
                return
            self.invocations[ident] = self.invocations.get(ident, 0) + 1
            self.pre_spawn[ident] = total
            started = self.time.isoformat()
            self._status(path, job, state='RUNNING', worker=worker, started_at=started)
            reporting.job_event(job, worker, 'RUNNING', 'trước khi spawn worker')
            state['attempts'] = state['runs_total'] = total
            state['runs_total_shared_key'] = total
            state['effective_max_runs'] = limit
            state['final_worker'] = worker
            kind, detail, result = self._outcome(step)
            code = 0 if kind in {'ok', 'qc_fail'} or kind.startswith('ok_') else 1
            if kind == 'real':
                evidence = Path(__file__).resolve().parent / 'selftest' / 'RESULT_SANDBOX_REAL.json'
                real = json.loads(evidence.read_text(encoding='utf-8')) if evidence.exists() else {}
                if real.get('ok') and real.get('classified_type') == 'NEEDS_HUMAN':
                    reason = 'sandbox thật (codex read-only) bị từ chối: ' + str(
                        (real.get('actual_sandbox_error') or ['read-only'])[-1])[:120]
                else:
                    self.unsupported.append('requires_real_run')
                    reason = 'chưa có bằng chứng chạy sandbox thật'
                reporting.job_event(job, worker, 'BLOCKED', reason)
                self._escalate(ident, job, 'NEEDS_HUMAN', reason, 'sandbox_real', runs=total)
                return
            if kind == 'hang':
                code = 1
                detail = 'stall: không phản hồi trong max_runtime'
            if kind == 'ok':
                output = Path(job['signals'][0][5:])
                payload = result or {'status': 'pass', 'summary': 'RESULT hoàn tất', 'metric': 1}
                output.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                if total > 1:
                    payload = {**payload, 'self_healed_runs': total - 1}
                    output.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                reporting.job_event(job, worker, 'DONE', 'đủ tín hiệu kết quả')
                self._status(path, job, state='DONE_NEEDS_REVIEW', worker=worker,
                             finished_at=self.time.isoformat(), error_type=None, attempts=total)
                state['status'] = 'DONE_NEEDS_REVIEW'
                state['last_qc'] = 'pass'
                state['attempts'] = total
                self.latency[ident] = (self.time - self.zero).total_seconds() / 60
                normal_outbox.enqueue(path)
                return
            tail = detail
            cause = detail if kind == 'hang' else None
            missing = []
            if kind in {'ok_no_file', 'ok_empty_file'}:
                missing = ['file:' + job['signals'][0][5:]]
                if kind == 'ok_empty_file':
                    Path(job['signals'][0][5:]).write_bytes(b'')
            if kind == 'ok_status_pass_no_metric':
                missing = ['qc:thiếu số đo metric']
            if kind == 'qc_fail':
                missing = ['qc:' + detail]
            if kind == 'sandbox':
                tail = 'blocked by policy: ' + detail
            if kind == 'transient':
                tail = 'timeout: ' + detail
            if kind == 'quota':
                tail = 'hit your limit: ' + detail
            classification = error_classifier.classify(job['worker'], tail, code, missing, cause, self.patterns)
            forced = {'sandbox': 'NEEDS_HUMAN', 'needs_human': 'NEEDS_HUMAN', 'quota': 'QUOTA',
                      'transient': 'TRANSIENT', 'unknown': 'UNKNOWN', 'qc_fail': 'QC_FAIL',
                      'ok_no_file': 'FALSE_DONE', 'ok_empty_file': 'FALSE_DONE',
                      'ok_status_pass_no_metric': 'QC_FAIL', 'hang': classification['type']}
            if kind in forced:
                classification['type'] = forced[kind]
            human_type = classification['type'] == 'NEEDS_HUMAN'
            note = 'stall' if kind == 'hang' else tail
            reporting.job_event(job, worker, 'BLOCKED', note)
            self.signatures[ident] = classification['signature']
            if not human_type:
                try:
                    record = worker_health.record(job['worker'], classification['signature'], key=ident)
                    if record.get('opened'):
                        self.breaker_pending[job['worker']] = (
                            '[THÔNG BÁO] ngắt mạch: worker ' + job['worker']
                            + ' tạm nghỉ 60 phút do lỗi lặp cùng chữ ký')
                except Exception:
                    pass
            same = total - 1
            counts = self.type_counts.setdefault(ident, {})
            counts[classification['type']] = counts.get(classification['type'], 0) + 1
            occurrence = counts[classification['type']]
            learned = self._apply_learned(tail, ident)
            if learned and learned.get('escalate'):
                classification['type'] = 'ESCALATE_LEARNED'
                classification['signature'] = learned['signature']
            elif learned:
                classification['type'] = learned['type']
                classification['approved_action'] = learned['action']
                classification['signature'] = learned['signature']
            if classification.get('approved_action') == 'ignore_benign':
                self._status(path, job, state='DONE_NEEDS_REVIEW', worker=worker,
                             finished_at=self.time.isoformat(), attempts=total)
                state['status'] = 'DONE_NEEDS_REVIEW'
                state['attempts'] = total
                state['last_qc'] = 'pass'
                self.latency[ident] = (self.time - self.zero).total_seconds() / 60
                return
            t = classification['type']
            if t in {'NEEDS_HUMAN', 'ESCALATE_LEARNED'}:
                action = 'ESCALATE'
            elif t == 'QUOTA':
                action = 'FALLBACK' if job['fallback_workers'] else 'ESCALATE'
            elif t in {'FALSE_DONE', 'UNKNOWN'}:
                action = 'RETRY' if occurrence < 2 else 'ESCALATE'
            else:
                action = 'RETRY' if occurrence < 3 else 'ESCALATE'
            if classification.get('approved_action') == 'retry':
                action = 'RETRY'
            elif classification.get('approved_action') == 'fallback':
                action = 'FALLBACK'
            elif classification.get('approved_action') in {'needs_human', 'ignore_benign'}:
                action = 'ESCALATE'
            if job['requires_owner_approval'] and action in {'RETRY', 'FALLBACK'}:
                action = 'ESCALATE'
            if total >= limit and action in {'RETRY', 'FALLBACK'}:
                action = 'ESCALATE'
            target = None
            if action == 'FALLBACK':
                chain = [job['worker']] + job['fallback_workers']
                position = chain.index(worker) if worker in chain else 0
                target = next((x for x in chain[position + 1:] if x != worker), None)
                allowed = False
                if target:
                    allowed, _ = recovery_policy.check(job, 'FALLBACK', target, policy_path=self.policy)
                if not allowed:
                    action, target = 'ESCALATE', None
            est = float(self.jobs[ident].get('est_cost_per_run', 0) or 0)
            reporting.recovery(job, classification, action, target or worker, note,
                               policy_detail={'estimated_cost_usd': est})
            if action in {'RETRY', 'FALLBACK'}:
                if action == 'FALLBACK' and target:
                    self.actions.append('QUOTA->FALLBACK ' + target)
                    worker = target
                    state['fallback_switches'] += 1
                else:
                    self.actions.append('%s->AUTO-RETRY#%d (%s)'
                                        % (t, total, t))
                if kind == 'qc_fail':
                    self.retry_briefs[ident] = 'Lần trước trượt tiêu chí đo: ' + detail
                elif kind in {'ok_no_file', 'ok_empty_file'}:
                    self.retry_briefs[ident] = ('BẮT BUỘC ghi tệp kết quả ' + job['signals'][0])
                elif kind == 'ok_status_pass_no_metric':
                    self.retry_briefs[ident] = 'BẮT BUỘC bổ sung số đo metric còn thiếu'
                if t == 'TRANSIENT':
                    delay = [30, 120][min(delay_index, 1)]
                    self.retry_delays.setdefault(ident, []).append(delay)
                    delay_index += 1
                continue
            self._escalate(ident, job, 'NEEDS_HUMAN' if human_type else 'ESCALATE',
                           tail, classification['signature'], runs=total)
            return

    def _run_disabled(self, ident, job, path, state):
        script = list(self.jobs[ident].get('script', []))
        step = script[0] if script else 'ok'
        kind, detail, result = self._outcome(step)
        try:
            run_counter.reserve(job, job['worker'], 'INITIAL')
        except Exception:
            pass
        self.invocations[ident] = self.invocations.get(ident, 0) + 1
        self.pre_spawn[ident] = 1
        state['attempts'] = 1
        state['runs_total'] = 1
        state['final_worker'] = job['worker']
        if kind not in {'sandbox', 'needs_human'}:
            Path(job['signals'][0][5:]).write_text(json.dumps(result or {'status': 'pass'}),
                                                   encoding='utf-8')
            self._status(path, job, state='DONE_NEEDS_REVIEW', attempts=1,
                         finished_at=self.time.isoformat())
            state['status'] = 'DONE_NEEDS_REVIEW'
        else:
            self._status(path, job, state='BLOCKED', attempts=1,
                         finished_at=self.time.isoformat())
            state['status'] = 'BLOCKED'
            state['status_class'] = 'FAILED_LEGACY_BEHAVIOR'

    def _apply_learned(self, tail, ident):
        text = (tail or '').lower()
        data = json.loads(self.patterns.read_text(encoding='utf-8'))
        learned = data.get('learned', {})
        for signature, item in learned.items():
            if signature.lower() not in text:
                continue
            state_name = item.get('state')
            if state_name in {'REJECTED', 'EXPIRED'}:
                return None
            created = reporting.parse(item.get('approved_at'))
            age_ok = (not created) or (self.time - created) <= timedelta(days=7)
            if state_name == 'PENDING_CONFIRMATION':
                if not age_ok:
                    item['state'] = item['status'] = 'EXPIRED'
                    self.patterns.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                    return None
                if item.get('action') in {'retry', 'ignore_benign'}:
                    if int(item.get('applied_count', 0)) < 3:
                        item['applied_count'] = int(item.get('applied_count', 0)) + 1
                        self.patterns.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                        self.state[ident]['learned_applications'] = self.state[ident].get('learned_applications', 0) + 1
                        return {'signature': signature, 'type': item.get('type') or 'TRANSIENT',
                                'action': item['action']}
                    self.state[ident].setdefault('learned_applications', 0)
                    return {'signature': signature, 'escalate': True}
                return None
            if state_name == 'CONFIRMED':
                if item.get('action') == 'retry' or item.get('action') == 'rework':
                    return {'signature': signature, 'type': item.get('type') or 'TRANSIENT',
                            'action': item['action']}
                if item.get('action') == 'fallback':
                    return {'signature': signature, 'type': 'QUOTA', 'action': 'fallback'}
                return {'signature': signature, 'escalate': True}
            return None
        return None

    def _start_refused(self, ident, job, reason):
        path = self.jobs_dir / ident
        self._status(path, job, state='BLOCKED', error_type='NEEDS_HUMAN', reason=reason,
                     error_signature='policy-lock', finished_at=self.time.isoformat())
        state = self.state[ident]
        state['status'] = 'BLOCKED'
        state['status_class'] = 'NEEDS_HUMAN'
        state['attempts'] = 0
        self.actions.append('START REFUSED: ' + reason)
        self.human_worker = job['worker']
        human_outbox.enqueue(path, reporting.read(path / 'status.json'),
                             sender=self._send_human, clock=self._clock,
                             outbox=self.human, needs=self.needs)
        state['owner_notified'] = reporting.read(path / 'status.json').get('owner_notified', False)

    def _escalate(self, ident, job, category, reason, signature, runs=None):
        path = self.jobs_dir / ident
        note = None if runs is None else f'đã chạy {runs}/3'
        self._status(path, job, state='BLOCKED', error_type=category, reason=reason,
                     error_signature=signature, finished_at=self.time.isoformat(),
                     runs_display=note)
        state = self.state[ident]
        state['status'] = 'BLOCKED'
        state['status_class'] = 'NEEDS_HUMAN' if category == 'NEEDS_HUMAN' else 'ESCALATED'
        self.latency[ident] = (self.time - self.zero).total_seconds() / 60
        self.human_worker = job['worker']
        human_outbox.enqueue(path, reporting.read(path / 'status.json'),
                             sender=self._send_human, clock=self._clock,
                             outbox=self.human, needs=self.needs)
        state['owner_notified'] = reporting.read(path / 'status.json').get('owner_notified', False)

    def _send_human(self, text):
        # One alert per signature: buffer the human text so the end of the batch
        # can fold a circuit-breaker notice into it instead of sending a second
        # message for the same root cause. A failed transport is reported now so
        # human_outbox keeps the item pending for retry (never lost).
        if self._telegram_mode() != 'ok':
            return 99
        self.human_buffer.append((self.human_worker, text))
        return 0

    def _flush_human(self):
        buffered, self.human_buffer = self.human_buffer, []
        for worker, text in buffered:
            note = self.breaker_pending.pop(worker, None)
            self._send(text + ('\n' + note if note else ''))
        for note in self.breaker_pending.values():
            self._send(note)
        self.breaker_pending = {}

    def _flush_normal(self):
        normal_outbox.flush(sender=self._send, clock=self._clock, outbox=self.normal,
                            sent=self.sent, force=True)
        for row in reporting.rows(self.normal):
            pass
        for ident in self.jobs:
            path = self.jobs_dir / ident
            self.state[ident]['owner_notified'] = reporting.read(path / 'status.json').get('owner_notified', False)

    def _finish_active(self):
        try:
            while self.active:
                ident = self.active.pop(0)
                if ident in self.finished:
                    continue
                self.finished.add(ident)
                item = self.jobs[ident]
                if item.get('legacy'):
                    path = self.jobs_dir / ident
                    job = reporting.read(path / 'job.json')
                    self._status(path, job, state='DONE_NEEDS_REVIEW', legacy=True,
                                 finished_at=self.time.isoformat())
                    self.state[ident]['status'] = 'DONE_NEEDS_REVIEW'
                    continue
                self.run_job(ident)
        finally:
            self._flush_human()
            self._flush_normal()

    # ----- components --------------------------------------------------------
    def _tick(self, component):
        if component == 'supervisor':
            self._finish_active()
        elif component == 'watchdog':
            self._watchdog()
        elif component in {'digest', 'digest_force'}:
            digest_module.run(sender=self._send, clock=self._clock, log=self.digest_log,
                              force=component == 'digest_force')
            human_outbox.deliver(sender=self._send, clock=self._clock, outbox=self.human)
        elif component == 'check_checker':
            check_the_checker.run(now=self.time, sender=self._send, heart=self.heart,
                                  state=self.checker_state)
        else:
            self.unsupported.append('tick:' + component)

    def _watchdog(self):
        import watchdog
        watchdog.run(jobs=self.jobs_dir, sender=self._send, sent=self.sent, clock=self._clock,
                     legacy_file=self.legacy_file, reminder_file=self.reminders)

    def _ao_status(self):
        warnings = ao_status.collect(jobs=self.jobs_dir, clock=self._clock)
        text = 'AO STATUS: ' + ('CÓ CẢNH BÁO' if warnings else 'sạch')
        lines = [text] + warnings[:23]
        if len(warnings) > 23:
            lines.append(f'... và {len(warnings) - 23} cảnh báo khác')
        output = '\n'.join(lines)
        self.ao_status_outputs.append({'text': output, 'exit_code': 1 if warnings else 0})
        return output

    # ----- events ------------------------------------------------------------
    def run(self):
        output = None
        try:
            for event in self.case.get('events', []):
                if not self.machine_off:
                    self.time = self.zero + timedelta(minutes=float(event.get('t', 0)))
                self._event(event)
            output = self._assemble()
        finally:
            self._restore()
        return output

    def _event(self, event):
        op = event['op']
        if op == 'start_job':
            if not self.machine_off:
                self._start(event['job'])
        elif op in {'finish_all', 'run_until_idle'}:
            if not self.machine_off:
                self._finish_active()
        elif op == 'rerun_all':
            for ident in list(self.finished):
                pass
        elif op == 'tick':
            if not self.machine_off:
                self._tick(event.get('component'))
        elif op == 'machine_off':
            self.machine_off = True
        elif op == 'machine_on':
            self.machine_off = False
        elif op == 'crash_process':
            point = event.get('point')
            if point == 'after_counter_increment_before_spawn':
                self.crash = point
            elif point == 'after_api_ok_before_mark':
                self._crash_after_api()
        elif op == 'concurrent':
            for sub in event.get('ops', []):
                self._event(sub)
        elif op == 'force_started_at':
            self._force_started(event['job'], event['iso'])
        elif op == 'learn':
            self._learn(event.get('args', {}))
        elif op == 'confirm_learned':
            self._confirm(event.get('id'), event.get('owner_quote'))
        elif op == 'reject_learned':
            self._reject(event.get('id'), event.get('quote') or event.get('owner_quote'))
        elif op == 'owner_reject_learned':
            self._reject_signature(event.get('signature'), event.get('quote'))
        elif op == 'counter_reset':
            self._counter_reset(event)
        elif op == 'edit_policy':
            self._edit_policy(event)
        elif op == 'flag_urgent':
            self._flag_urgent(event)
        elif op == 'ao_status':
            self._ao_status()
        else:
            self.unsupported.append(op)

    def _force_started(self, ident, iso):
        path = self.jobs_dir / ident
        job = reporting.read(path / 'job.json')
        self._status(path, job, state='DONE_NEEDS_REVIEW', legacy=False, started_at=iso,
                     finished_at=self.time.isoformat())
        self.state[ident]['status'] = 'DONE_NEEDS_REVIEW'

    def _crash_after_api(self):
        rows = reporting.rows(self.human)
        for row in reversed(rows):
            if row.get('status') == 'delivered':
                rollback = dict(row)
                rollback['status'] = 'pending'
                rollback['next_retry'] = self.time.isoformat()
                reporting.append(self.human, rollback, durable=True)
                break

    def _learn(self, args):
        source = args.get('quote_source')
        quote = args.get('owner_quote')
        if source and source != 'owner_chat':
            self.actions.append('LEARN REJECTED (quote_source)')
            return
        signature = args.get('signature', '')
        if signature.startswith('@sig_of:'):
            signature = self.signatures.get(signature.split(':', 1)[1], signature)
        if not any(row.get('signature') == signature for row in reporting.rows(self.unknown)):
            reporting.append(self.unknown, {'at': self.time.isoformat(), 'signature': signature,
                                            'worker': 'unknown', 'reason': 'adapter seed'}, durable=True)
        data = json.loads(self.patterns.read_text(encoding='utf-8'))
        if args.get('force') and signature in data.get('learned', {}):
            history = data.setdefault('lessons_history', [])
            history.append({'signature': signature, **data['learned'][signature]})
            self.patterns.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            learn.resolve(signature, args.get('action'), quote, args.get('note', ''),
                          args.get('auto') == 'yes', args.get('force', False),
                          self.patterns, self.unknown)
            self.actions.append('LEARN ' + signature)
        except Exception as exc:
            self.actions.append('LEARN REJECTED: ' + str(exc)[:80])

    def _find_by_id(self, ident):
        data = json.loads(self.patterns.read_text(encoding='utf-8')).get('learned', {})
        for signature, item in data.items():
            if item.get('id') == ident:
                return signature
        return ident

    def _confirm(self, ident, quote):
        signature = self._find_by_id(ident)
        try:
            learn.change(signature, 'CONFIRMED', quote, self.patterns)
        except Exception as exc:
            self.actions.append('CONFIRM REJECTED: ' + str(exc)[:80])

    def _reject(self, ident, quote):
        signature = self._find_by_id(ident)
        try:
            learn.change(signature, 'REJECTED', quote, self.patterns)
        except Exception as exc:
            self.actions.append('REJECT REJECTED: ' + str(exc)[:80])

    def _reject_signature(self, signature, quote):
        try:
            learn.change(signature, 'REJECTED', quote, self.patterns)
        except Exception as exc:
            self.actions.append('REJECT REJECTED: ' + str(exc)[:80])

    def _counter_reset(self, event):
        if event.get('quote_source') and event['quote_source'] != 'owner_chat':
            return
        try:
            counter_admin.reset(event['key'], event.get('owner_quote', ''), self.counter)
        except Exception as exc:
            self.actions.append('RESET REJECTED: ' + str(exc)[:80])

    def _edit_policy(self, event):
        data = json.loads(self.policy.read_text(encoding='utf-8'))
        data[event['field']] = event['value']
        self.policy.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        if event.get('update_lock'):
            self._refresh_lock()
        self.actions.append('EDIT POLICY ' + event['field'])

    def _flag_urgent(self, event):
        reason = str(event.get('reason'))
        label = {'data_leak': 'lộ dữ liệu'}.get(reason, reason)
        body = ('[KHẨN] ' + label + ': ' + str(event.get('detail'))
                + '\ncần bạn xem ngay')
        self._send(body)

    # ----- reporting ---------------------------------------------------------
    def _assemble(self):
        if not self.machine_off:
            pass
        for ident in self.jobs:
            path = self.jobs_dir / ident
            self.state[ident]['owner_notified'] = reporting.read(path / 'status.json').get('owner_notified', False)
            self.state[ident]['finished_min'] = self.latency.get(ident)
        try:
            counter = run_counter.read(self.counter)['groups']
        except Exception:
            counter = {}
        legacy = {ident: ident for ident, item in self.jobs.items()
                  if item.get('legacy') or reporting.legacy(self.jobs_dir / ident,
                                                            reporting.read(self.jobs_dir / ident / 'status.json').get('attempt_id') or ident + '#1')}
        self.legacy_file.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding='utf-8')
        history = json.loads(self.patterns.read_text(encoding='utf-8')).get('lessons_history', [])
        learned = json.loads(self.patterns.read_text(encoding='utf-8')).get('learned', {})
        history_sigs = {h.get('signature') for h in history}
        learned_entries = [{'signature': key, 'action': item.get('action'), 'state': item.get('state'),
                            'quote': item.get('approved_by_owner_quote'),
                            'approved_by': item.get('approved_by'),
                            'applied_count': item.get('applied_count', 0),
                            'history_keeps_previous': key in history_sigs,
                            'id': item.get('id')} for key, item in learned.items()]
        human_rows = [r for r in reporting.rows(self.human) if r.get('lane') == 'human']
        latest_human = {}
        for row in human_rows:
            latest_human[row['escalation_id']] = row
        self.state['human_outbox_undelivered'] = sum(r['status'] not in {'delivered', 'deduped'}
                                                     for r in latest_human.values())
        self.state['needs_human_md_contains'] = [ident for ident in self.jobs
                                                 if ident in (self.needs.read_text(encoding='utf-8')
                                                              if self.needs.exists() else '')]
        sent_rows = [r for r in reporting.rows(self.sent) if r.get('ok')]
        self.state['sent_ledger_entries_total'] = len(sent_rows)
        self.state['sent_ledger_entries_for'] = {
            ident: sum(r.get('id') == ident and r.get('ok') for r in reporting.rows(self.sent))
            for ident in self.jobs}
        self.state['signature_counts_total'] = sum(human_outbox.signature_counts(self.human).values())
        self.state['legacy_json_contains'] = [ident for ident in legacy]
        self.state['legacy_json_contains_count'] = len(legacy)
        self.state['learned_entries'] = learned_entries
        self.state['learned_entry_count'] = len(learned_entries)
        for item in learned_entries:
            self.state['learned_entry_' + item['signature']] = item
        health = worker_health.read(self.health)
        self.state['workers'] = {name: {'breaker': 'open' if worker_health.is_open(
            name, self.time, health, self.health) else 'closed', 'rest_min': 60}
            for name in health.get('workers', {})}
        self.state['run_counter'] = {key: row['runs_total'] for key, row in counter.items()}
        self.state['run_counter_entries'] = sum(len(row.get('runs', [])) for row in counter.values())
        self.state['all_jobs_status'] = None
        if not self.disabled:
            try:
                self._ao_status()
            except Exception:
                pass
        files = {}
        if not self.disabled:
            for label, path in (('tools/ao-report/sent.jsonl', self.sent),
                                ('tools/ao-report/JOBS.log', self.job_log),
                                ('tools/ao-report/outbox/normal.jsonl', self.normal),
                                ('tools/ao-report/outbox/human.jsonl', self.human),
                                ('tools/ao-report/needs_human.md', self.needs),
                                ('tools/ao-report/recovery.jsonl', self.recovery),
                                ('tools/ao-report/error_patterns_unknown.jsonl', self.unknown),
                                ('tools/ao-report/state/run_counter.json', self.counter),
                                ('tools/ao-report/legacy.json', self.legacy_file),
                                ('tools/ao-report/heartbeat.json', self.heart),
                                ('tools/ao-report/learned_audit.jsonl', self.audit),
                                ('tools/ao-report/state/counter_audit.jsonl', self.ar / 'state' / 'counter_audit.jsonl'),
                                ('tools/ao-report/digest.jsonl', self.digest_log)):
                files[label] = path.read_text(encoding='utf-8') if path.exists() else ''
        touched = list(files) + (['tools/ao-report/JOBS.log'] if not self.disabled else [])
        output = {'messages': self.messages, 'state': self.state, 'files': files,
                  'files_touched': touched,
                  'runs_per_job': {k: v.get('runs_total', 0) for k, v in self.state.items()
                                   if k in self.jobs},
                  'worker_invocations': self.invocations,
                  'counter_on_disk_at_spawn': self.pre_spawn,
                  'retry_briefs': self.retry_briefs, 'retry_delays_s': self.retry_delays,
                  'original_briefs_unchanged': all(
                      (self.jobs_dir / ident / 'brief.md').read_bytes() == blob
                      for ident, blob in self.original_briefs.items()),
                  'outbound_hosts': self.outbound_hosts, 'actions_log': self.actions,
                  'coordinator_calls': len(self.coordinator_calls), 'errors': self.errors,
                  'ao_status_outputs': self.ao_status_outputs,
                  'digest_texts': self.digest_texts,
                  'learned_audit': [r for r in reporting.rows(self.audit)],
                  'counter_audit': [r for r in reporting.rows(self.ar / 'state' / 'counter_audit.jsonl')],
                  'logical_keys': {key: row.get('ids', []) for key, row in counter.items()},
                  'start_refused': any('START REFUSED' in a for a in self.actions),
                  'unsupported_checks': sorted(set(self.unsupported))}
        return _stable(output, self.root)


def _stable(value, root):
    if isinstance(value, str):
        value = value.replace(str(root), '<SANDBOX>')
        value = value.replace(str(root).replace('\\', '/'), '<SANDBOX>')
        value = re.sub(r'\b[0-9a-f]{40,}\b', '<SHA>', value)
        value = re.sub(r'\b[0-9a-f]{20}\b', '<EID>', value)
        value = re.sub(r'\b[0-9a-f]{12}\b', '<HASH>', value)
        value = re.sub(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+', '<TS>', value)
        return value
    if isinstance(value, list):
        return [_stable(item, root) for item in value]
    if isinstance(value, dict):
        return {_stable(key, root) if isinstance(key, str) else key: _stable(item, root)
                for key, item in value.items()}
    return value
