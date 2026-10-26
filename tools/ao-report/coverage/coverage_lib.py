"""Shared, machine-written coverage ledger for work started OUTSIDE ao_run.

AUTOREPORT (watchdog.py) only sees jobs that ran through ao_run. This ledger
records the other ways work starts in a session so a *machine* — not an AI's
memory — can answer "was this subagent / background job / scheduled task ever
reported to the Owner?".

Design rules (Owner directive 11/10/2026):
  * Append-only JSONL, one JSON object per line, file-locked (msvcrt on Windows).
  * FAIL-OPEN everywhere: a broken ledger, a read-only disk, or an internal
    error must NEVER block a tool call or a hook. Every writer swallows errors.
  * Readable with zero dependencies (stdlib only) so the watchdog and
    ao_status can read it without an AI session.

Record kinds (see README.md for the full table):
  spawn / launched / finish agent (subagent) start, async launch and end
  subagent_stop             secondary subagent-completion signal (best effort)
  bg_start                  a run-in-background command was started
  reported                  coordinator confirmed it told the Owner (mark_reported.py)
  reminded                  a Stop-hook reminder was emitted for that id
  local_wrapped             a long command was wrapped via run_local.py
  scheduled                 a scheduled task logged a run (log_scheduled.py)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

COVERAGE = Path(__file__).resolve().parent
HERE = COVERAGE.parent                      # tools/ao-report
DEFAULT_LEDGER = COVERAGE / 'ledger.jsonl'
RESULT_LIMIT = 200
HANG_MINUTES = 15                            # Stop-hook: spawned this long w/o finish => remind
REPORT_MINUTES = 30                          # watchdog/digest: finished this long w/o report
STALE_OPEN_MINUTES = 30                      # watchdog/digest: open this long => suspect hang
MAX_REMINDS = 2                              # per id, hard cap; never remind forever

AGENT_TOOLS = {'agent', 'task', 'subagent'}
BG_TOOLS = {'bash', 'powershell', 'run_shell_command', 'shell'}

TOKEN = re.compile(r'(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}')
PRIVATE = re.compile(r'(?i)(?:\.worker-keys|telegram\.env|key[_-]?file|token|secret|api[_-]?key|password)')

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass


def enabled():
    return os.environ.get('AO_COVERAGE', '1').strip().lower() not in {'0', 'false', 'off'}


def ledger_path():
    return Path(os.environ.get('AO_COVERAGE_LEDGER', str(DEFAULT_LEDGER)))


def now():
    return datetime.now(timezone.utc)


def parse(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def clean(value, limit=180):
    """Mask tokens / sensitive paths and flatten whitespace; never raise."""
    try:
        text = TOKEN.sub('[ẩn]', str(value or ''))
        text = re.sub(r'[\r\n\t]+', ' ', text).strip()
        if PRIVATE.search(text):
            return '[ẩn nội dung nhạy cảm]'
        return text[:limit]
    except Exception:
        return ''


@contextmanager
def _locked(path):
    """Best-effort cross-process lock; if anything fails we proceed unlocked."""
    msvcrt = None
    try:
        import msvcrt as _m
        msvcrt = _m
    except Exception:
        msvcrt = None
    handle = None
    try:
        Path(str(path) + '.lock').parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path) + '.lock', 'a+b')
        if msvcrt:
            handle.seek(0)
            deadline = time.monotonic() + 3
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
    except Exception:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                if msvcrt:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass


def append(record, path=None):
    """Append one record. Returns True/False; NEVER raises (fail-open)."""
    try:
        target = Path(path or ledger_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload.setdefault('at', now().isoformat())
        line = json.dumps(payload, ensure_ascii=False)
        with _locked(target):
            with target.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
        return True
    except Exception:
        return False


def read(path=None):
    """Tolerant reader: a corrupt or missing ledger yields []."""
    target = Path(path or ledger_path())
    try:
        text = target.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _new_item(ident, kind, desc='', agent_type='', session=None):
    return {'id': ident, 'kind': kind, 'desc': desc or '', 'agent_type': agent_type or '',
            'session': session, 'spawned_at': None, 'finished_at': None, 'status': None,
            'result': '', 'reported_at': None, 'reminds': 0, 'last_remind_at': None}


def _earliest(candidate, current):
    if current is None:
        return candidate
    a = candidate.get('spawned_at') or datetime.max.replace(tzinfo=timezone.utc)
    b = current.get('spawned_at') or datetime.max.replace(tzinfo=timezone.utc)
    return candidate if a < b else current


def items(records=None):
    """Replay the ledger into {id: item}. Pure, tolerant of junk records."""
    records = read() if records is None else records
    out = {}
    for row in records:
        kind = row.get('kind')
        ident = row.get('id') or row.get('agent_id')
        at = parse(row.get('at'))
        session = row.get('session')
        if kind in ('spawn', 'bg_start'):
            it = out.setdefault(ident, _new_item(ident, 'agent' if kind == 'spawn' else 'bg',
                                                 session=session))
            if it['spawned_at'] is None:
                it['spawned_at'] = at
            if row.get('desc'):
                it['desc'] = row['desc']
            if row.get('agent_type'):
                it['agent_type'] = row['agent_type']
            if it.get('session') is None:
                it['session'] = session
        elif kind == 'launched':
            it = out.setdefault(ident, _new_item(ident, 'agent', session=session))
            if it['spawned_at'] is None:
                it['spawned_at'] = at
            if row.get('agent_id'):
                it['agent_id'] = row['agent_id']
            if row.get('desc'):
                it['desc'] = row['desc']
        elif kind in ('finish', 'bg_finish'):
            it = out.setdefault(ident, _new_item(ident, 'agent' if kind == 'finish' else 'bg',
                                                 session=session))
            it['finished_at'] = at
            it['status'] = row.get('status')
            it['result'] = clean(row.get('result', ''), RESULT_LIMIT)
        elif kind == 'subagent_stop':
            candidate = None
            for it in out.values():
                if it['kind'] != 'agent':
                    continue
                if session is not None and it.get('session') != session:
                    continue
                if it['spawned_at'] and at and it['spawned_at'] > at:
                    continue
                if it.get('agent_id') and it['agent_id'] == row.get('agent_id'):
                    candidate = it if it['finished_at'] is None else None
                    break
                if it['finished_at'] is not None or it.get('agent_id'):
                    continue
                candidate = _earliest(it, candidate)
            if candidate is not None:
                candidate['finished_at'] = at
                if candidate['status'] is None:
                    candidate['status'] = row.get('status') or 'stopped'
                if not candidate['result']:
                    candidate['result'] = clean(row.get('result', ''), RESULT_LIMIT)
        elif kind == 'reported':
            it = out.get(ident)
            if it is not None:
                it['reported_at'] = at
        elif kind == 'reminded':
            it = out.get(ident)
            if it is not None:
                it['reminds'] = int(it.get('reminds', 0)) + 1
                it['last_remind_at'] = at
    return out


def due_reminders(state, clock=None, hang_minutes=HANG_MINUTES, max_reminds=MAX_REMINDS,
                  session_id=None):
    """Items the in-session Stop hook should nag about: finished-but-unreported,
    or open past `hang_minutes`. Respects the per-id reminder cap.
    A supplied session excludes other sessions and legacy sessionless items;
    None preserves the unfiltered behavior for callers without session context."""
    clock = clock or now()
    out = []
    for it in state.values():
        if session_id is not None and it.get('session') != session_id:
            continue
        if int(it.get('reminds', 0)) >= max_reminds:
            continue
        if it['finished_at'] is not None:
            if it['reported_at'] is None:
                out.append(('unreported', it))
        elif it['spawned_at'] is not None and it['kind'] != 'bg' and it['reported_at'] is None:
            if clock - it['spawned_at'] >= timedelta(minutes=hang_minutes):
                out.append(('hang', it))
    out.sort(key=lambda pair: pair[1]['spawned_at'] or datetime.max.replace(tzinfo=timezone.utc))
    return out


def stale(state, clock=None, report_minutes=REPORT_MINUTES, open_minutes=STALE_OPEN_MINUTES):
    """Items the out-of-session watchdog/digest should surface."""
    clock = clock or now()
    out = {'unreported': [], 'open': []}
    for it in state.values():
        if it['finished_at'] is not None and it['reported_at'] is None:
            if (clock - it['finished_at']).total_seconds() / 60 >= report_minutes:
                out['unreported'].append(it)
        elif it['finished_at'] is None and it['spawned_at'] is not None and it['kind'] != 'bg':
            if (clock - it['spawned_at']).total_seconds() / 60 >= open_minutes:
                out['open'].append(it)
    return out


def open_items(state, clock=None):
    """Currently-open agent items (spawned, no finish) — informational only."""
    clock = clock or now()
    return [it for it in state.values() if it['finished_at'] is None and it['spawned_at'] is not None]


def reminder_reason(due, clock=None, limit=6):
    """Human-readable reason string for the Stop hook. Masks sensitive text."""
    clock = clock or now()
    lines = ['[AUTOREPORT phủ sóng] Máy tự đối chiếu sổ việc NGOÀI ao_run (không phải AI tự nhớ):']
    shown = due[:limit]
    for tag, it in shown:
        when = (it['finished_at'] if tag == 'unreported' else it['spawned_at'])
        minutes = int((clock - when).total_seconds() // 60) if when else 0
        label = clean(it.get('desc') or it.get('agent_type') or it['id'], 60)
        if tag == 'unreported':
            lines.append(f"  • xong {minutes} phút nhưng CHƯA báo Owner: {label} [{it['id']}]")
        else:
            lines.append(f"  • mở {minutes} phút chưa có kết quả (nghi treo): {label} [{it['id']}]")
    if len(due) > limit:
        lines.append(f'  • … và {len(due) - limit} mục khác')
    lines.append('Báo Owner rồi chạy: python tools\\ao-report\\coverage\\mark_reported.py <id> '
                 '(tối đa nhắc 2 lần/mục).')
    return '\n'.join(lines)


def summary(clock=None):
    """Compact numbers for the digest / ao_status."""
    clock = clock or now()
    state = items()
    st = stale(state, clock)
    return {
        'unreported': len(st['unreported']),
        'suspect_hang': len(st['open']),
        'open_total': len(open_items(state, clock)),
        'latest_unreported': [clean(it.get('desc') or it['id'], 50) for it in st['unreported'][:3]],
        'latest_open': [clean(it.get('desc') or it['id'], 50) for it in st['open'][:3]],
    }


def describe_tool_input(tool_input, limit=160):
    """Pick a short, readable label from a tool_input dict."""
    if not isinstance(tool_input, dict):
        return ''
    for key in ('description', 'subagent_type', 'command', 'prompt'):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return clean(value, limit)
    return ''


def summarise_response(response):
    """Return (result_text<=200, status) from a PostToolUse tool_response."""
    status = None
    if isinstance(response, dict):
        if response.get('is_error') or response.get('isError') or response.get('error'):
            status = 'error'
        for key in ('summary', 'result', 'output', 'text', 'content', 'stdout'):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return clean(value, RESULT_LIMIT), status or 'ok'
            if isinstance(value, list):
                parts = [b.get('text') for b in value if isinstance(b, dict) and isinstance(b.get('text'), str)]
                if parts:
                    return clean(' '.join(parts), RESULT_LIMIT), status or 'ok'
        return '', status or 'ok'
    if isinstance(response, str):
        return clean(response, RESULT_LIMIT), status or 'ok'
    return '', status or 'ok'
