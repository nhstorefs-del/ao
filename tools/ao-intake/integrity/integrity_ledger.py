"""Append-only, verified hash chain for approved configuration snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEFAULT_LEDGER = HERE / 'baseline.jsonl'
GENESIS = '0' * 64
HEX = re.compile(r'^[0-9a-f]{64}$')
DEFAULT_PROTECTED = [
    '.claude/settings.json', '.claude/settings.local.json', '.claude/hooks/**/*',
    'tools/ao-report/coverage/hook_*.py', 'tools/ao-report/coverage/coverage_lib.py',
    'tools/ao-report/session_start_status.py',
    'tools/ao-intake/approval_hook.py', 'tools/ao-intake/approval_ledger.py',
    'tools/ao-intake/approval_gate.py', 'tools/ao-intake/integrity/*.py',
    'tools/ao-intake/integrity/protected.json',
]


def canonical(row):
    return json.dumps({k: v for k, v in row.items() if k != 'this_hash'},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(row):
    return hashlib.sha256(canonical(row)).hexdigest()


def read(path=DEFAULT_LEDGER):
    path = Path(path)
    if not path.exists():
        return []
    rows, previous = [], GENESIS
    with path.open('rb') as stream:
        for number, raw in enumerate(stream, 1):
            if not raw.endswith(b'\n'):
                raise ValueError(f'sổ đường chuẩn bị cắt ở dòng {number}')
            try:
                row = json.loads(raw.decode('utf-8'))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f'sổ đường chuẩn hỏng ở dòng {number}') from exc
            if not isinstance(row, dict) or set(row) != {'seq', 'at', 'files', 'approved_by', 'reason', 'prev_hash', 'this_hash'}:
                raise ValueError(f'bản ghi đường chuẩn sai cấu trúc ở dòng {number}')
            files = row['files']
            if (type(row['seq']) is not int or row['seq'] != number or row['prev_hash'] != previous
                    or not isinstance(files, dict) or not all(isinstance(k, str) and HEX.fullmatch(v or '') for k, v in files.items())
                    or not isinstance(row['approved_by'], str) or not row['approved_by']
                    or not isinstance(row['reason'], str) or not row['reason'].strip()
                    or not isinstance(row['at'], str) or not HEX.fullmatch(row['this_hash'] or '')
                    or digest(row) != row['this_hash']):
                raise ValueError(f'chuỗi băm đường chuẩn sai ở dòng {number}')
            try:
                datetime.fromisoformat(row['at'])
            except ValueError as exc:
                raise ValueError(f'thời điểm sai ở dòng {number}') from exc
            rows.append(row)
            previous = row['this_hash']
    return rows


def snapshot(root=ROOT, config=None):
    root = Path(root).resolve()
    config = Path(config) if config else root / 'tools/ao-intake/integrity/protected.json'
    patterns = DEFAULT_PROTECTED
    if config.exists():
        value = json.loads(config.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('patterns'), list) or not value['patterns']:
            raise ValueError('protected.json cần {"patterns": ["..."]}')
        patterns = value['patterns']
    files = {}
    for pattern in patterns:
        if (not isinstance(pattern, str) or not pattern or '\\' in pattern or pattern.startswith('/')
                or ':' in pattern or any(part == '..' for part in pattern.split('/'))):
            raise ValueError('mẫu đường dẫn không hợp lệ')
        for path in root.glob(pattern):
            if not path.is_file() or '__pycache__' in path.parts or any('.bak' in part for part in path.parts):
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError('tệp bảo vệ nằm ngoài gốc')
            key = resolved.relative_to(root).as_posix()
            files[key] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return dict(sorted(files.items()))


def append(files, approved_by, reason, path=DEFAULT_LEDGER):
    """Caller must hold approval and baseline locks and validate authorization."""
    rows = read(path)
    row = {'seq': len(rows) + 1, 'at': datetime.now(timezone.utc).isoformat(),
           'files': files, 'approved_by': approved_by, 'reason': reason.strip(),
           'prev_hash': rows[-1]['this_hash'] if rows else GENESIS}
    row['this_hash'] = digest(row)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as stream:
        stream.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    return row
