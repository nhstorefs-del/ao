"""Fail-open Claude Code PreToolUse warning for direct worker execution."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from directrun_lib import detect, safe_command

BASE = Path(__file__).resolve().parent
WARNING = 'Cảnh báo: chạy thẳng thợ ngoài ao_run — dùng ao_run start'
DENIAL = 'Từ chối chạy thẳng thợ ngoài ao_run; hãy dùng ao_run start.'


def mode(base: Path = BASE) -> str:
    flag = base / 'MODE'
    value = flag.read_text(encoding='utf-8').strip() if flag.exists() else 'warn'
    if value == 'off':
        return 'off'
    if value == 'block' and not (base / 'ALLOW_ONCE').exists():
        return 'block'
    return 'warn'


def process(payload: dict, *, base: Path = BASE, environ=None) -> dict:
    env = os.environ if environ is None else environ
    if env.get('AO_DIRECTRUN_HOOK') == '0' or 'AO_INTAKE_REQUEST' in env or 'AO_RUN_ID' in env:
        return {}
    if not isinstance(payload, dict) or payload.get('hook_event_name') != 'PreToolUse':
        return {}
    tool = str(payload.get('tool_name') or '')
    if tool.lower() not in {'bash', 'powershell'}:
        return {}
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get('command'), str):
        return {}
    command = tool_input['command']
    reason = detect(command)
    if not reason:
        return {}
    setting = mode(base)
    if setting == 'off':
        return {}
    path = base / 'data' / 'directrun.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {'at': datetime.now(timezone.utc).isoformat(),
           'session_id': str(payload.get('session_id') or '')[:100],
           'tool': tool, 'command': safe_command(command), 'reason': reason,
           'source': 'live',
           'mode': setting}
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    if setting == 'block':
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                       'permissionDecision': 'deny',
                                       'permissionDecisionReason': DENIAL}}
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                   'additionalContext': WARNING}}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    try:
        payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))
        result = process(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        try:
            path = BASE / 'logs' / 'directrun_hook.log'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(f'{datetime.now(timezone.utc).isoformat()} {type(exc).__name__}\n')
        except Exception:
            pass
        sys.stdout.write('{}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
