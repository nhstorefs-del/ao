"""Detect direct worker CLI invocations in Claude shell tool input."""
from __future__ import annotations

import re

SEPARATORS = {';', '&&', '||', '|', '\n'}
SHELLS = {'bash', 'powershell'}
PYTHON = {'python', 'python.exe', 'python3', 'python3.exe', 'py', 'py.exe'}
READ_ONLY = {'echo', 'printf', 'grep', 'rg', 'findstr', 'write-output'}
SECRET = re.compile(
    r'(?i)(?:sk-[a-z0-9_-]{12,}|gh[pous]_[a-z0-9]{20,}|'
    r'xox[abprs]-[a-z0-9-]{10,}|AIza[0-9a-z_-]{20,}|'
    r'AKIA[0-9A-Z]{12}|[0-9]{5,}:[a-z0-9_-]{20,}|[A-Za-z0-9+/_-]{40,}={0,2})'
)
ASSIGNMENT = re.compile(r'(?i)((?:--?\S*(?:token|secret|password|passwd|api[_-]?key)|'
                        r'(?:token|secret|password|passwd|api[_-]?key))\s*(?:=|:)\s*)([^\s;|&]+)')
SPACED_SECRET = re.compile(r'(?i)((?<!\w)--?(?:api[_-]?key|token|secret|password|passwd)\s+)([^\s;|&]+)')
BEARER = re.compile(r'(?i)(\bBearer\s+)([A-Za-z0-9._~+/-]+)')
ENV_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z_0-9]*=')


def safe_command(command: str) -> str:
    """Redact before truncation so secrets beyond the limit cannot leak."""
    line = re.sub(r'[\r\n\t]+', ' ', command)
    line = ASSIGNMENT.sub(lambda m: m.group(1) + '[ĐÃ CHE]', line)
    line = SPACED_SECRET.sub(lambda m: m.group(1) + '[ĐÃ CHE]', line)
    line = BEARER.sub(lambda m: m.group(1) + '[ĐÃ CHE]', line)
    line = SECRET.sub('[ĐÃ CHE]', line)
    return line[:200]


def tokens(command: str):
    """Small shell lexer: quotes keep separators and words as data."""
    out, word, quote = [], '', None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == '\\' and quote == '"' and i + 1 < len(command) and command[i + 1] in ('"', '\\'):
                i += 1
                word += command[i]
            else:
                word += ch
        elif ch in ('"', "'"):
            quote = ch
        elif ch in ' \t\r':
            if word:
                out.append(word)
                word = ''
        elif ch in ';|&\n':
            if word:
                out.append(word)
                word = ''
            if ch in '|&' and i + 1 < len(command) and command[i + 1] == ch:
                out.append(ch + ch)
                i += 1
            else:
                out.append(ch)
        else:
            word += ch
        i += 1
    if word:
        out.append(word)
    return out


def basename(value: str) -> str:
    return value.replace('\\', '/').rsplit('/', 1)[-1].lower()


def cli_name(value: str) -> str:
    return re.sub(r'\.(?:exe|cmd|bat|ps1)$', '', basename(value))


def detect(command: str) -> str | None:
    """Return a reason only for an actual direct invocation."""
    if not isinstance(command, str):
        return None
    all_tokens = tokens(command)
    if any(basename(t) in {'ao_run.py', 'ao_supervisor.py'} for t in all_tokens):
        return None
    if any(re.match(r'(?i)^\$?(?:env:)?(?:AO_INTAKE_REQUEST|AO_RUN_ID)=', t) for t in all_tokens):
        return None
    segments = []
    segment = []
    for token in all_tokens + [';']:
        if token in SEPARATORS:
            if segment:
                segments.append(segment)
            segment = []
        else:
            segment.append(token)
    for seg in segments:
        while seg and (seg[0] in {'&', 'env', 'command', 'nohup'} or ENV_ASSIGN.match(seg[0])):
            seg = seg[1:]
        if not seg:
            continue
        head = cli_name(seg[0])
        if head in READ_ONLY:
            continue
        args = seg[1:]
        if '--help' in args or '--version' in args or head.endswith('help'):
            continue
        if head in PYTHON:
            args = [a for a in args if not a.startswith('-')]
            if args and basename(args[0]) in {'haiku_worker.py', 'supervisor.py'}:
                return basename(args[0])
        if head in {'haiku_worker.py', 'supervisor.py', 'supervisor', 'supervisor.exe'}:
            return head
        if head == 'codex' and (not args or args[0] not in {'help', 'completion'}):
            return 'codex'
        if head in {'qwen', 'deepseek', 'deepseek-cli'}:
            return head
        if head == 'claude' and any(a in {'-p', '--print'} for a in args):
            return 'claude'
    return None
