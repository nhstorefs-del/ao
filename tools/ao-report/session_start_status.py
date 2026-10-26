"""SessionStart hook: inject a short, read-only AO status at the start of every
session (startup / resume / clear / compact) so no one has to remember to call
ao_status.  FAIL-OPEN: any error prints nothing and exits 0.  Switch off with
AO_SESSIONSTART=0.  Writes nothing; sends nothing."""
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_LINES = 40
HERE = Path(__file__).resolve().parent


def main():
    if os.environ.get('AO_SESSIONSTART', '1').lower() in {'0', 'false', 'off'}:
        return 0
    try:
        try:
            sys.stdin.read()
        except Exception:
            pass
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}
        done = subprocess.run([sys.executable, '-B', str(HERE / 'ao_status.py')], capture_output=True,
                              timeout=10, env=env, cwd=str(HERE))
        text = done.stdout.decode('utf-8', errors='replace').strip()
        if not text:
            return 0
        lines = text.splitlines()
        shown = lines[:MAX_LINES]
        if len(lines) > MAX_LINES:
            shown.append(f'... (cắt, còn {len(lines) - MAX_LINES} dòng; chạy tools\ao-report\ao_status.py)')
        body = ('[AO STATUS đầu phiên — máy tự chạy, chỉ đọc, không phải AI tự nhớ]\n' + '\n'.join(shown))
        out = {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': body}}
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
