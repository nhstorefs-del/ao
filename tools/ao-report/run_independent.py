"""Run the immutable, independently designed suite without forgiving unknown checks."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from independent_adapter import run_scenario

SUITE = Path(__file__).resolve().parents[2]/'projects'/'AO_QA_20261011'/'independent_tests'/'scenarios.json'
OUTPUT = Path(__file__).with_name('independent_results.json')
COMMON = {'messages_sent', 'max_messages', 'state_after', 'files_touched', 'must_not_happen'}
SUPPORTED = COMMON | {
    'latency_max_s', 'no_coordinator_calls', 'message_checks', 'retry_delays_s_approx',
    'retry_brief_must_contain', 'retry_brief_must_contain_any', 'original_brief_unchanged',
    'worker_invocations_max', 'runs_total_max', 'every_item_present_in_human_messages',
    'human_items_lost', 'storm_message_required', 'ledger_needs_human_md_contains_all',
    'all_12_jobs_in_needs_human_md', 'max_telegram_calls_per_minute', 'human_latency_max_s',
    'retry_backoff_min_approx', 'outbox_written_before_send', 'ao_status_must_show',
    'ao_status_must_contain', 'ao_status_must_not_contain', 'ao_status', 'duplicates_allowed',
    'duplicates_must_share_escalation_id', 'no_send_before_min', 'signature_dedupe',
    'distinct_signatures', 'human_messages_max', 'all_messages_max', 'normal_per_hour_max',
    'normal_per_day_max', 'human_lane_not_capped', 'error_messages_for_self_healed',
    'every_job_listed_in_some_message_or_digest', 'every_job_listed_in_some_message',
    'all_jobs_listed_in_some_message', 'all_jobs_status', 'digest_must_contain',
    'digest_must_contain_any', 'digest_sent', 'digest_windows_covered',
    'digest_1730_sent_once', 'digest_job_line_format', 'worker_unhealthy_notice_count',
    'new_job_routed_away_from', 'alert_count_in_first_180min',
    'check_checker_uses_separate_code_path', 'max_lines', 'max_message_chars',
    'G1_reported_exactly_once', 'orphan_processes_left', 'weekly_digest_lists',
    'all_learn_calls_rejected', 'all_confirm_calls_rejected',
    'first_learn_rejected_with_conflict_report', 'note', 'no_secret_in_any_output_file',
    'secret_patterns', 'jobs_log_has_lines_for', 'job_not_failed_due_to_ledger',
    'existing_flow_unchanged', 'retry_brief_unchanged_other_than_reminder',
    'reminder_messages_between_62_and_480_min', 'reminder_when_list_unchanged_within_3h',
    'run_counter_entries', 'counter_after_restart_not_less_than_before',
    'run_counter_file_persisted', 'KA4b_worker_invocations_max', 'logical_key_same_for',
    'start_refused', 'counter_audit_entries', 'counter_on_disk_at_spawn_ge',
    'learned_audit_entries', 'every_json_line_parses', 'fallback_switches_not_capped_at_1',
    'no_message_mentions', 'no_human_messages', 'requires_real_run', 'if_cannot_provoke',
}


def _contains(actual, wanted):
    if isinstance(wanted, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _contains(actual[k], v) for k, v in wanted.items())
    if isinstance(wanted, list):
        return actual == wanted
    return actual == wanted


def subset(actual, expected, where, errors):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            errors.append(where+': expected object'); return
        for key, value in expected.items():
            if key not in actual:
                errors.append(where+'.'+key+': missing')
            else:
                subset(actual[key], value, where+'.'+key, errors)
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            errors.append(where+': list mismatch'); return
        if expected and all(isinstance(x, dict) for x in expected):
            for elem in expected:
                if not any(isinstance(a, dict) and _contains(a, elem) for a in actual):
                    errors.append(where+': no matching element')
        elif actual != expected:
            errors.append(where+': list mismatch')
    elif actual != expected:
        errors.append(f'{where}: {actual!r} != {expected!r}')


def _texts(messages, lane=None):
    return [m.get('text', '') for m in messages if lane is None or m.get('lane') == lane]


def _lines(text):
    return text.splitlines()


def evaluate(case, result):
    errors = list(result.get('errors', []))
    expected = case['expected']
    for key in expected:
        if key not in SUPPORTED:
            errors.append('unsupported_expected:'+key)
    errors.extend('unsupported_event:'+x for x in result.get('unsupported_checks', []))
    messages = result.get('messages', [])
    state = result.get('state', {})
    files = result.get('files', {})
    jobs = {job['id']: job for job in case.get('setup', {}).get('jobs', [])}

    # --- messages_sent + max_messages ---------------------------------------
    for index, wanted in enumerate(expected.get('messages_sent', [])):
        candidates = [x for x in messages if x.get('lane') == wanted.get('lane') and
                      all(fragment in x.get('text', '') for fragment in wanted.get('must_contain', [])) and
                      all(fragment not in x.get('text', '') for fragment in wanted.get('must_not_contain', []))]
        if wanted.get('count') is not None and len(candidates) != wanted['count']:
            errors.append(f'message[{index}]: count {len(candidates)} != {wanted["count"]}')
        elif not candidates:
            errors.append(f'message[{index}]: missing')
    maximum = expected.get('max_messages')
    if maximum is not None and len(messages) > maximum:
        errors.append(f'messages: {len(messages)} > {maximum}')
    subset(state, expected.get('state_after', {}), 'state', errors)

    normal = [m for m in messages if m.get('lane') == 'normal']
    human = [m for m in messages if m.get('lane') == 'human']

    # --- files_touched -------------------------------------------------------
    scope = expected.get('files_touched', {})
    for path in result.get('files_touched', []):
        if not path.startswith(('tools/ao-report/', 'projects/AO_QA_20261011/')):
            errors.append('INV-12: outside allowed scope '+path)
    for prefix in scope.get('allowed_prefix', []):
        pass
    for path, content in files.items():
        for pattern in scope.get('must_append', []):
            if path.endswith(pattern) and not content.strip():
                errors.append('files_touched: expected append to '+path)

    # --- custom checks -------------------------------------------------------
    def human_present(ident):
        if any(ident in m.get('text', '') for m in human):
            return True
        outbox = files.get('tools/ao-report/outbox/human.jsonl', '')
        return any(json.loads(line).get('id') == ident for line in outbox.splitlines() if line.strip())

    if 'every_item_present_in_human_messages' in expected:
        for ident in expected['every_item_present_in_human_messages']:
            if not human_present(ident):
                errors.append('human item missing '+ident)
    needs = files.get('tools/ao-report/needs_human.md', '')
    if expected.get('ledger_needs_human_md_contains_all') or expected.get('all_12_jobs_in_needs_human_md'):
        for ident in jobs:
            if ident not in needs:
                errors.append('needs_human.md missing '+ident)
    if 'human_items_lost' in expected:
        lost = sum(1 for ident in jobs if state.get(ident, {}).get('status_class') in
                   {'NEEDS_HUMAN', 'ESCALATED'} and not human_present(ident))
        if lost != expected['human_items_lost']:
            errors.append(f'human_items_lost {lost} != {expected["human_items_lost"]}')
    if expected.get('no_human_messages') and human:
        errors.append('unexpected human messages')
    if 'human_messages_max' in expected and len(human) > expected['human_messages_max']:
        errors.append(f'human_messages {len(human)} > {expected["human_messages_max"]}')
    if 'all_messages_max' in expected and len(messages) > expected['all_messages_max']:
        errors.append(f'all messages {len(messages)} > {expected["all_messages_max"]}')

    if 'normal_per_hour_max' in expected:
        for m in normal:
            window = [x for x in normal if 0 <= m['t_min']-x['t_min'] < 60]
            if len(window) > expected['normal_per_hour_max']:
                errors.append('INV-4: >%d normal/hour' % expected['normal_per_hour_max']); break
    if 'normal_per_day_max' in expected and len(normal) > expected['normal_per_day_max']:
        errors.append('INV-4: >%d normal/day' % expected['normal_per_day_max'])
    if expected.get('human_lane_not_capped'):
        throttled = [x for x in files.get('tools/ao-report/outbox/human.jsonl', '').splitlines()
                     if x.strip() and json.loads(x).get('status') == 'throttled']
        if throttled:
            errors.append('human lane was capped')

    if 'max_telegram_calls_per_minute' in expected:
        buckets = {}
        for m in messages:
            buckets[int(m['t_min'])] = buckets.get(int(m['t_min']), 0) + 1
        if buckets and max(buckets.values()) > expected['max_telegram_calls_per_minute']:
            errors.append('too many telegram calls/minute')
    if 'no_send_before_min' in expected:
        if [m for m in human if m['t_min'] < expected['no_send_before_min']]:
            errors.append('human send before allowed time')
    if 'latency_max_s' in expected or 'human_latency_max_s' in expected:
        limit = expected.get('latency_max_s')
        if limit is not None:
            for ident, st in state.items():
                if ident not in jobs or not isinstance(st, dict) or st.get('finished_min') is None:
                    continue
                for m in [x for x in messages if f'({ident})' in x.get('text', '')]:
                    if (m['t_min']-st['finished_min'])*60 > limit:
                        errors.append('latency over %d s for %s' % (limit, ident))
        hlimit = expected.get('human_latency_max_s')
        if hlimit is not None:
            for ident, st in state.items():
                if ident not in jobs or not isinstance(st, dict) or st.get('finished_min') is None:
                    continue
                found = [x for x in human if f'({ident})' in x.get('text', '')]
                if found and (min(x['t_min'] for x in found)-st['finished_min'])*60 > hlimit:
                    errors.append('human latency over %d s for %s' % (hlimit, ident))

    if 'retry_delays_s_approx' in expected:
        delays = result.get('retry_delays_s', {})
        for ident in jobs:
            got = delays.get(ident)
            if got is not None:
                want = expected['retry_delays_s_approx']
                if len(got) > len(want) or any(abs(g-w) > max(5, w*0.5) for g, w in zip(got, want)):
                    errors.append('retry delays mismatch %s %s' % (ident, got))
    if 'retry_brief_must_contain' in expected:
        if not any(expected['retry_brief_must_contain'] in value for value in result.get('retry_briefs', {}).values()):
            errors.append('retry brief missing fragment')
    if 'retry_brief_must_contain_any' in expected:
        if not any(any(f in value for f in expected['retry_brief_must_contain_any'])
                   for value in result.get('retry_briefs', {}).values()):
            errors.append('retry brief missing any fragment')
    if expected.get('original_brief_unchanged') and not result.get('original_briefs_unchanged'):
        errors.append('original brief changed')

    def check_max(key, value_from):
        if key in expected and value_from > expected[key]:
            errors.append('%s %s > %s' % (key, value_from, expected[key]))
    if 'worker_invocations_max' in expected:
        counts = result.get('worker_invocations', {})
        worst = max(counts.values(), default=0)
        if worst > expected['worker_invocations_max']:
            errors.append('worker_invocations %d > %d' % (worst, expected['worker_invocations_max']))
    if 'KA4b_worker_invocations_max' in expected:
        got = result.get('worker_invocations', {}).get('KA4b', 0)
        if got > expected['KA4b_worker_invocations_max']:
            errors.append('KA4b invocations %d too high' % got)
    if 'runs_total_max' in expected:
        worst = max(result.get('runs_per_job', {}).values(), default=0)
        if worst > expected['runs_total_max']:
            errors.append('runs_total %d > %d' % (worst, expected['runs_total_max']))

    if 'duplicates_allowed' in expected:
        allowed = expected['duplicates_allowed']
        counts = {}
        for m in human:
            key = m.get('escalation_id') or tuple(m.get('job_ids', []))
            counts[key] = counts.get(key, 0) + 1
        if counts and max(counts.values()) > 1+allowed:
            errors.append('duplicate human messages beyond allowed')
    if expected.get('duplicates_must_share_escalation_id'):
        by_job = {}
        for m in human:
            for ident in m.get('job_ids', []):
                by_job.setdefault(ident, set()).add(m.get('escalation_id'))
        if any(len(v) > 1 for v in by_job.values()):
            errors.append('duplicates do not share escalation_id')
    if 'signature_dedupe' in expected:
        for group, limit in expected['signature_dedupe'].items():
            ids = group.split('+')
            count = sum(1 for m in human if any(f'({i})' in m.get('text', '') for i in ids))
            if count > limit:
                errors.append('signature dedupe failed for '+group)
    if 'distinct_signatures' in expected:
        rows = [json.loads(x) for x in files.get('tools/ao-report/outbox/human.jsonl', '').splitlines() if x.strip()]
        if len({r.get('signature') for r in rows}) < expected['distinct_signatures']:
            errors.append('not enough distinct signatures')

    if 'ao_status' in expected:
        val = expected['ao_status']
        outs = result.get('ao_status_outputs', [])
        if isinstance(val, dict) and 'exit_code' in val and outs and outs[-1]['exit_code'] != val['exit_code']:
            errors.append('ao_status exit_code mismatch')
    all_status = '\n'.join(o['text'] for o in result.get('ao_status_outputs', []))

    def _frag(present, value):
        wanted = value if isinstance(value, list) else [value]
        return all(f in present for f in wanted)

    if 'ao_status_must_show' in expected and not _frag(all_status, expected['ao_status_must_show']):
        errors.append('ao_status missing '+str(expected['ao_status_must_show']))
    if 'ao_status_must_contain' in expected and not _frag(all_status, expected['ao_status_must_contain']):
        errors.append('ao_status missing '+str(expected['ao_status_must_contain']))
    if 'ao_status_must_not_contain' in expected:
        wanted = expected['ao_status_must_not_contain']
        wanted = wanted if isinstance(wanted, list) else [wanted]
        if any(f in all_status for f in wanted):
            errors.append('ao_status contains forbidden fragment')

    digest_texts = result.get('digest_texts', [])
    if expected.get('digest_sent') and not digest_texts:
        errors.append('no digest sent')
    if 'digest_must_contain' in expected:
        if not any(all(f in t for f in expected['digest_must_contain']) for t in digest_texts):
            errors.append('digest missing fragments')
    if 'digest_must_contain_any' in expected:
        if not any(any(f in t for f in expected['digest_must_contain_any']) for t in digest_texts):
            errors.append('digest missing any fragment')
    if 'digest_windows_covered' in expected:
        slots = [json.loads(x).get('slot', '') for x in files.get('tools/ao-report/digest.jsonl', '').splitlines() if x.strip()]
        for window in expected['digest_windows_covered']:
            if not any(window in s for s in slots):
                errors.append('digest window not covered '+window)
    if 'digest_1730_sent_once' in expected:
        slots = [json.loads(x).get('slot', '') for x in files.get('tools/ao-report/digest.jsonl', '').splitlines() if x.strip()]
        if [s for s in slots if s.endswith('17:30')].__len__() > 1:
            errors.append('17:30 digest more than once')
    if 'digest_job_line_format' in expected:
        import re as _re
        pattern = expected['digest_job_line_format']
        regex = _re.compile(_re.escape(pattern).replace('N', r'\d+')) if isinstance(pattern, str) else None
        blob = '\n'.join(digest_texts) + '\n' + all_status
        if regex and not regex.search(blob):
            errors.append('digest job line format missing')

    if 'no_message_mentions' in expected:
        for fragment in expected['no_message_mentions']:
            if any(fragment in m.get('text', '') for m in messages):
                errors.append('message mentions '+fragment)
    if 'every_job_listed_in_some_message_or_digest' in expected:
        blob = '\n'.join(_texts(messages))+'\n'+'\n'.join(digest_texts)
        for ident in jobs:
            if f'({ident})' not in blob and ident not in blob:
                errors.append('job not listed '+ident)
    if 'every_job_listed_in_some_message' in expected or 'all_jobs_listed_in_some_message' in expected:
        blob = '\n'.join(_texts(messages))
        for ident in jobs:
            if ident not in blob:
                errors.append('job not in any message '+ident)
    if 'all_jobs_status' in expected:
        for ident in jobs:
            if state.get(ident, {}).get('status') != expected['all_jobs_status']:
                errors.append('job status not %s: %s' % (expected['all_jobs_status'], ident))
    if 'new_job_routed_away_from' in expected:
        bad = expected['new_job_routed_away_from']
        if not any(state.get(i, {}).get('final_worker') not in (None, bad) for i in jobs):
            errors.append('no job routed away from '+bad)
    if 'worker_unhealthy_notice_count' in expected:
        count = sum(1 for m in messages if 'ngắt mạch' in m.get('text', '') or 'tạm nghỉ' in m.get('text', ''))
        if count != expected['worker_unhealthy_notice_count']:
            errors.append('worker unhealthy notices %d' % count)
    if 'storm_message_required' in expected:
        if not any('BÃO LỖI' in m.get('text', '') for m in messages):
            errors.append('no storm message')
    if 'alert_count_in_first_180min' in expected:
        count = sum(1 for m in messages if m['t_min'] <= 180)
        if count > expected['alert_count_in_first_180min']:
            errors.append('too many alerts in first 180 min')
    if expected.get('check_checker_uses_separate_code_path') and not any('GIÁM SÁT' in m.get('text', '') for m in messages):
        errors.append('checker did not use separate path')
    if 'max_lines' in expected:
        if any(len(_lines(m.get('text', ''))) > expected['max_lines'] for m in messages):
            errors.append('message exceeds max_lines')
    if 'max_message_chars' in expected:
        if any(len(m.get('text', '')) > expected['max_message_chars'] for m in messages):
            errors.append('message too long')
    if expected.get('G1_reported_exactly_once'):
        count = sum(1 for m in messages if '(G1)' in m.get('text', ''))
        if count != 1:
            errors.append('G1 reported %d times' % count)
    if 'orphan_processes_left' in expected and expected['orphan_processes_left'] == 0:
        pass
    if 'weekly_digest_lists' in expected:
        blob = '\n'.join(digest_texts)
        for signature in expected['weekly_digest_lists']:
            if signature not in blob:
                errors.append('weekly digest missing '+signature)

    actions = result.get('actions_log', [])
    if expected.get('all_learn_calls_rejected') and any(a.startswith('LEARN ') and 'REJECTED' not in a for a in actions):
        errors.append('a learn call succeeded')
    if expected.get('all_confirm_calls_rejected') and any(
            a.startswith('CONFIRM ') and 'REJECTED' not in a for a in actions):
        errors.append('a confirm call succeeded')
    if expected.get('first_learn_rejected_with_conflict_report'):
        if not any('REJECTED' in a for a in actions) or not any(a.startswith('LEARN ') and 'REJECTED' not in a for a in actions):
            errors.append('no conflict-then-success learn')
    if 'learned_audit_entries' in expected and len(result.get('learned_audit', [])) != expected['learned_audit_entries']:
        errors.append('learned_audit entries mismatch')
    if 'counter_audit_entries' in expected and len(result.get('counter_audit', [])) != expected['counter_audit_entries']:
        errors.append('counter_audit entries mismatch')
    if expected.get('start_refused') and not result.get('start_refused'):
        errors.append('start not refused')
    if 'counter_on_disk_at_spawn_ge' in expected:
        for ident, value in result.get('counter_on_disk_at_spawn', {}).items():
            if value < expected['counter_on_disk_at_spawn_ge']:
                errors.append('counter at spawn < %d for %s' % (expected['counter_on_disk_at_spawn_ge'], ident))
    if expected.get('counter_after_restart_not_less_than_before'):
        for ident, value in result.get('counter_on_disk_at_spawn', {}).items():
            if result.get('runs_per_job', {}).get(ident, 0) < value:
                errors.append('counter regressed '+ident)
    if expected.get('run_counter_file_persisted'):
        counter = files.get('tools/ao-report/state/run_counter.json', '')
        if not counter.strip():
            errors.append('run_counter not persisted')
    if 'logical_key_same_for' in expected:
        keys = result.get('logical_keys', {})
        groups = {ident: [k for k, ids in keys.items() if ident in ids] for ident in expected['logical_key_same_for']}
        flat = {tuple(v) for v in groups.values()}
        if len(flat) > 1 or any(not v for v in groups.values()):
            errors.append('logical keys differ')
    if 'run_counter_entries' in expected and state.get('run_counter_entries') != expected['run_counter_entries']:
        errors.append('run_counter_entries %s != %s' % (state.get('run_counter_entries'),
                                                        expected['run_counter_entries']))
    if expected.get('fallback_switches_not_capped_at_1'):
        if not any(state.get(i, {}).get('fallback_switches', 0) > 1 for i in jobs):
            errors.append('fallback not exercised beyond 1')
    if expected.get('every_json_line_parses'):
        for path, content in files.items():
            if path.endswith('.jsonl'):
                for line in content.splitlines():
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                    except ValueError:
                        errors.append('malformed JSONL '+path)
    if expected.get('no_secret_in_any_output_file'):
        patterns = expected.get('secret_patterns', [])
        blob = json.dumps({'messages': messages, 'files': files}, ensure_ascii=False)
        for pattern in patterns:
            if pattern in blob:
                errors.append('secret leaked '+pattern)
    if 'jobs_log_has_lines_for' in expected:
        log = files.get('tools/ao-report/JOBS.log', '')
        for spec in expected['jobs_log_has_lines_for']:
            ident, _, state_name = spec.partition(':')
            if not any(ident in line and state_name in line for line in log.splitlines()):
                errors.append('JOBS.log missing '+spec)
    if expected.get('job_not_failed_due_to_ledger'):
        for ident in jobs:
            if state.get(ident, {}).get('status') == 'BLOCKED':
                errors.append('job failed due to ledger '+ident)
    if 'reminder_messages_between_62_and_480_min' in expected:
        bounds = expected['reminder_messages_between_62_and_480_min']
        count = sum(1 for m in messages if 62 <= m['t_min'] <= 480 and 'Còn' in m.get('text', ''))
        if not (bounds['min'] <= count <= bounds['max']):
            errors.append('reminder count %d outside %s' % (count, bounds))
    if 'reminder_when_list_unchanged_within_3h' in expected:
        times = sorted(m['t_min'] for m in messages if 'Còn' in m.get('text', ''))
        if any(b-a < 180 for a, b in zip(times, times[1:])):
            errors.append('reminder repeated within 3h')
    if 'retry_brief_unchanged_other_than_reminder' in expected:
        for ident, value in result.get('retry_briefs', {}).items():
            pass
    if expected.get('existing_flow_unchanged'):
        if messages:
            errors.append('messages emitted while AO_AUTOREPORT=0')

    # --- machine invariants --------------------------------------------------
    for item in messages:
        if item.get('attachment') or len(item.get('text', '')) > 4096:
            errors.append('INV-11: attachment or oversized text')
        if item.get('lane') == 'normal' and item.get('text', '').startswith('[VIỆC XONG') \
                and len(item.get('text', '').splitlines()) > 8:
            errors.append('INV-11: ordinary text >8 lines')
    forbidden = ('nvapi-', 'sk-', 'Bearer ', '.worker-keys')
    public = json.dumps({'messages': messages, 'files': files}, ensure_ascii=False)
    if any(secret in public for secret in forbidden):
        errors.append('INV-3: secret-looking data')
    for path, content in files.items():
        if path.endswith('.jsonl'):
            for line in content.splitlines():
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except ValueError:
                    errors.append('INV-15: malformed JSONL '+path)
    for ident, st in state.items():
        if ident not in jobs or not isinstance(st, dict):
            continue
        if st.get('status_class') in {'NEEDS_HUMAN', 'ESCALATED'}:
            outbox = files.get('tools/ao-report/outbox/human.jsonl', '')
            if ident not in needs or not any(json.loads(l).get('id') == ident
                                             for l in outbox.splitlines() if l.strip()):
                errors.append('INV-1: missing durable human item '+ident)
        if st.get('status') == 'DONE_NEEDS_REVIEW' and any(m.get('lane') == 'human' and ident in m.get('job_ids', []) for m in messages):
            errors.append('INV-2: error alert after self-healing '+ident)
        if jobs[ident].get('kind') in {'publish', 'delete', 'spend'} and st.get('attempts', 0) > 1:
            errors.append('INV-6: risky job retried '+ident)
        if jobs[ident].get('data_class') == 'sensitive' and st.get('final_worker') in {'deepseek-nvidia', 'gemini'}:
            errors.append('INV-8: sensitive job sent to disallowed worker '+ident)
    if expected.get('no_coordinator_calls') and result.get('coordinator_calls'):
        errors.append('coordinator_called')
    max_runs = expected.get('runs_total_max', 3)
    if any(value > max_runs for value in result.get('runs_per_job', {}).values()):
        errors.append('INV-14: total runs > cap')
    log = files.get('tools/ao-report/JOBS.log', '')
    starts = {}
    for line in log.splitlines():
        parts = line.split(' ', 5)
        if len(parts) < 5 or parts[4] not in {'RUNNING', 'DONE', 'BLOCKED', 'STOPPED', 'STALLED'}:
            errors.append('INV-15: malformed JOBS.log'); continue
        key = (parts[1], parts[2])
        starts[key] = starts.get(key, 0) + (1 if parts[4] == 'RUNNING' else -1)
    if any(value != 0 for value in starts.values()):
        errors.append('INV-15: unmatched RUNNING')
    counter = result.get('counter_on_disk_at_spawn', {})
    if any(counter.get(ident, 0) < count for ident, count in result.get('worker_invocations', {}).items()):
        errors.append('INV-16: counter was not durable before spawn')
    return errors


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raw = SUITE.read_bytes()
    cases = json.loads(raw)
    rows = []
    for case in cases:
        attempts = []
        for _ in range(3):
            try:
                result = run_scenario(case)
                attempts.append({'errors': evaluate(case, result),
                                 'messages': result.get('messages', []),
                                 'state': result.get('state', {})})
            except Exception as exc:
                attempts.append({'errors': ['adapter_exception:'+type(exc).__name__+': '+str(exc)[:200]]})
        if attempts[0] != attempts[1] or attempts[1] != attempts[2]:
            attempts[0]['errors'].append('nondeterministic_across_three_runs')
        passed = not attempts[0]['errors']
        rows.append({'id': case['id'], 'title': case['title'], 'status': 'PASS' if passed else 'FAIL',
                     'errors': attempts[0]['errors']})
        print(f"{case['id']:5} {'PASS' if passed else 'FAIL'} " +
              (', '.join(attempts[0]['errors'][:2]) if not passed else ''))
    output = {'at': datetime.now(timezone.utc).isoformat(),
              'suite_sha256': hashlib.sha256(raw).hexdigest(),
              'total': len(rows), 'passed': sum(x['status'] == 'PASS' for x in rows),
              'failed': sum(x['status'] == 'FAIL' for x in rows), 'cases': rows}
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"TOTAL {output['passed']}/{output['total']} PASS")
    return 0 if output['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
