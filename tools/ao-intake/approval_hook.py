"""Unregistered UserPromptSubmit hook; it never writes to stdout."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from approval_ledger import Ledger, locked

BASE = Path(__file__).resolve().parent
PASTED = re.compile(r'<pasted_content\b[^>]*>.*?</pasted_content\b[^>]*>', re.I | re.S)  # the dong that kem id
SYSTEM_TAGS = 'system-reminder|task-notification|agent-message|cross-session-message|local-command-caveat|local-command-stdout|command-name|command-message|command-args'
SYSTEM_BLOCKS = re.compile(r'<(' + SYSTEM_TAGS + r')\b[^>]*>.*?(?:</\1\b[^>]*>|$)', re.I | re.S)
MARKER = re.compile(r'\b(?:APPROVAL|APROVAL)\s+BY\s+OWNER\b', re.I)
REQ = re.compile(r'\bREQ-[0-9a-fA-F]+\b', re.I)


SECRET = re.compile(r'(?i)(?:sk-[A-Za-z0-9_-]{16,}|gh[pous]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|[A-Za-z0-9+/_-]{40,}={0,2})')
LABELED = re.compile(r'(?i)((?:mật khẩu|mat khau|password|passwd|token|api[ _-]?key|secret|khóa)\s*[:=]\s*)(\S+)')


def safe_text(prompt):
    """Bản lưu của tin Owner: nội dung dán chỉ giữ độ dài + băm; chuỗi giống khóa/mật khẩu bị che."""
    import hashlib
    def paste(m):
        raw = m.group(0)
        return f'[nội dung dán {len(raw)} ký tự, sha256:{hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]}]'
    out = PASTED.sub(paste, prompt)
    out = LABELED.sub(lambda m: m.group(1) + '[ĐÃ CHE]', out)
    return SECRET.sub('[ĐÃ CHE]', out)


def process(payload, *, ledger=None, messages=None):
    if not isinstance(payload, dict) or not isinstance(payload.get('prompt'), str):
        raise ValueError('invalid prompt payload')
    prompt = payload['prompt']
    session = payload.get('session_id') or ''
    if not isinstance(session, str):
        session = ''
    # Khối do hệ thống/AI khác chèn vào (thông báo nền, nhắc hệ thống, tin từ phiên khác) KHÔNG phải lời Owner gõ.
    typed = SYSTEM_BLOCKS.sub('', PASTED.sub('', prompt))
    ids = list(dict.fromkeys('REQ-' + x[4:].lower() for x in REQ.findall(typed)))
    if MARKER.search(typed):
        (ledger or Ledger()).record(session, typed, ids)
        return 'approval'
    path = Path(messages or os.environ.get('AO_OWNER_MESSAGES') or BASE / 'data' / 'owner_messages.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'session_id': session,
                       'source': 'system' if not SYSTEM_BLOCKS.sub('', prompt).strip() else 'owner', 'text': safe_text(prompt)}, ensure_ascii=False) + '\n'
    with locked(path):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(line); stream.flush(); os.fsync(stream.fileno())
    return 'message'


def main():
    if os.environ.get('AO_APPROVAL_HOOK') == '0':
        return 0
    try:
        # Windows: stdin mặc định theo bảng mã máy (cp1252) làm hỏng tiếng Việt; luôn đọc UTF-8
        process(json.loads(sys.stdin.buffer.read().decode('utf-8', errors='replace')))
    except Exception as exc:
        try:
            path = Path(os.environ.get('AO_APPROVAL_HOOK_LOG') or BASE / 'logs' / 'approval_hook.log')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(f'{datetime.now(timezone.utc).isoformat()} {type(exc).__name__}\n')
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
