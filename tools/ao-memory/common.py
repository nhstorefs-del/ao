import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

_WINDOWS = os.name == "nt"
HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('AO_ROOT') or Path(__file__).resolve().parents[2]).expanduser().resolve()
MEMORY = Path(os.environ.get('AO_MEMORY_DIR') or ROOT / 'memory').expanduser().resolve()
SESSIONS = MEMORY.parent
JOURNAL = ROOT / 'tools/ao-journal'
DATA = HERE / 'data'
DATA.mkdir(exist_ok=True)

def hidden_popen_kwargs(*, detached=False, new_process_group=False, creationflags=0):
    """Hide console children; detached supervisors retain independent lifetime.

    Windows ignores CREATE_NO_WINDOW with DETACHED_PROCESS, so never combine
    them. STARTUPINFO also hides any startup window in the detached case.
    """
    if os.name != 'nt':
        return {}
    flags = creationflags
    if flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE):
        raise ValueError('Pass detached=True; do not supply console creation flags')
    if detached:
        flags &= ~subprocess.CREATE_NO_WINDOW
    flags |= subprocess.DETACHED_PROCESS if detached else subprocess.CREATE_NO_WINDOW
    if new_process_group:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs = {'creationflags': flags}
    if detached:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs['startupinfo'] = startup
    return kwargs

def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

def fold(s):
    s = unicodedata.normalize('NFD', str(s).casefold())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn').replace('đ', 'd')

def compact(s):
    return re.sub(r'[^a-z0-9]', '', fold(s))

def matches(query, value):
    a, b = fold(query), fold(value)
    if a in b or compact(query) in compact(value):
        return True
    aliases = {}  # tùy chọn: {'tên gọi khác': 'tênchuẩn'}
    return any(fold(k) in a and v in compact(value) for k, v in aliases.items())

SECRET = re.compile(r'(?i)(?:sk-[a-z0-9_-]{4,}|nvapi-[a-z0-9_-]{4,}|bearer\s+[^\s"\']+|api[_-]?key\s*[=:]\s*[^\s,"\']+)')
def redact(s):
    return SECRET.sub('[REDACTED]', str(s))

@contextlib.contextmanager
def locked(path, timeout=10):
    lock = Path(str(path) + '.lock')
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f'Không lấy được khóa: {lock}')
            time.sleep(.05)
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        # A Windows reader without FILE_SHARE_DELETE can briefly block rename.
        # Retry this same fully written temp; preserve the original on failure.
        delays = (.01, .02, .04) + (.08,) * 7
        for attempt in range(len(delays) + 1):
            try:
                os.replace(temp, path)
                break
            except OSError as exc:
                if not _WINDOWS or getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        Path(temp).unlink(missing_ok=True)

def read_json(path, default):
    path = Path(path)
    delays = (.01, .02, .04) + (.08,) * 7
    for attempt in range(len(delays) + 1):
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return default
        except PermissionError as exc:
            if not _WINDOWS or (getattr(exc, 'winerror', None) not in (5, 32, 33) and exc.errno != 13) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])

def process_start_time(pid):
    """Windows process creation FILETIME, or None when unavailable."""
    if os.name != 'nt': return None
    import ctypes
    from ctypes import wintypes
    try: pid = int(pid)
    except (TypeError, ValueError): return None
    if pid <= 0: return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return None
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)): return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally: kernel.CloseHandle(handle)

def pid_alive(pid, created=None):
    """Check a PID and, when stored, its Windows creation time."""
    try: pid = int(pid)
    except (TypeError, ValueError): return False
    if pid <= 0: return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle: return False
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259: return False
            if created is not None:
                times = (wintypes.FILETIME() for _ in range(4))
                started, exited, kernel_time, user_time = times
                kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
                kernel.GetProcessTimes.restype = wintypes.BOOL
                if not kernel.GetProcessTimes(handle, ctypes.byref(started), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)): return False
                return ((started.dwHighDateTime << 32) | started.dwLowDateTime) == int(created)
            return True
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid, 0); return True
    except (ProcessLookupError, PermissionError, OverflowError): return False

def ledger_state():
    import subprocess, sys
    p = subprocess.run([sys.executable, str(JOURNAL / 'queue.py'), 'state', '--json'],
                       cwd=str(HERE), capture_output=True, text=True, encoding='utf-8', timeout=4, **hidden_popen_kwargs())
    if p.returncode:
        raise RuntimeError(redact(p.stderr.strip()[:300]))
    return json.loads(p.stdout)


CODEX_VERSION = re.compile(r"codex-cli\s+(\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+(?:\.\d+)*)?)(?![\w.-])")

def codex_version_key(version):
    core, _, pre = version.partition('-')
    numbers = tuple(map(int, core.split('.')))
    if not pre: return numbers, 3, ()
    label, *parts = pre.split('.')
    return numbers, {'alpha': 0, 'beta': 1, 'rc': 2}[label], tuple(map(int, parts))

def probe_codex(exe, timeout=2):
    import subprocess
    try:
        result = subprocess.run([str(exe), '--version'], cwd=HERE,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout, **hidden_popen_kwargs())
        match = CODEX_VERSION.search(result.stdout) if result.returncode == 0 else None
        return match.group(1) if match else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def discover_codex():
    override = os.environ.get('AO_CODEX_EXE')
    if override is not None:
        exe = Path(override).expanduser().resolve()
        version = probe_codex(exe) if override.strip() and exe.is_file() else None
        if not version: raise ValueError('AO_CODEX_EXE không chạy được hoặc không trả phiên bản Codex hợp lệ')
        return str(exe), version, [(str(exe), version)]
    local = os.environ.get('LOCALAPPDATA')
    paths = []
    if local:
        base = Path(local)
        paths = sorted((base / 'OpenAI/Codex/bin').glob('*/codex.exe'))
        paths.append(base / 'Programs/OpenAI/Codex/bin/codex.exe')
    candidates = []
    for exe in paths:
        version = probe_codex(exe) if exe.is_file() else None
        if version: candidates.append((str(exe.resolve()), version))
    if not candidates: raise ValueError('Không tìm được Codex chạy được với phiên bản hợp lệ trong LOCALAPPDATA; có thể đặt AO_CODEX_EXE')
    exe, version = max(candidates, key=lambda item: codex_version_key(item[1]))
    return exe, version, candidates

def resolve_worker_command(command):
    command = list(command)
    version, candidates = None, []
    if command and command[0] == '{codex_latest}':
        command[0], version, candidates = discover_codex()
    return command, version, candidates
