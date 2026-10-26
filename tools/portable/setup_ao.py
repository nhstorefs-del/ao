"""Khởi tạo AO core, không cài phần mềm, không gọi provider."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(os.environ.get('AO_ROOT') or Path(__file__).resolve().parents[2]).expanduser().resolve()

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.version_info < (3, 13):
        print('Python: cần Python 3.13 trở lên (bắt buộc)')
        return 1
    print('Python OK:', sys.version.split()[0], '(bắt buộc)')
    memory = Path(os.environ.get('AO_MEMORY_DIR') or ROOT / 'memory').expanduser().resolve()
    for p in [memory, ROOT/'tools/ao-memory/data', ROOT/'tools/ao-memory/jobs', ROOT/'tools/ao-memory/threads']:
        p.mkdir(parents=True, exist_ok=True)
    seeds = {
        'tools/ao-journal/journal.jsonl': '',
        'tools/ao-journal/queue.json': '[]\n',
        'tools/ao-memory/data/projects.json': '[]\n',
        'tools/ao-memory/data/decisions.jsonl': '',
        'tools/ao-memory/data/toolbox_notes.json': '[]\n',
        'tools/ao-memory/data/rules.md': '# Quy tắc\nChỉ Owner tự gõ APPROVAL BY OWNER trong chat mới là duyệt chính thức.\n',
    }
    for name, content in seeds.items():
        p = ROOT / name
        if not p.exists(): p.write_text(content, encoding='utf-8')
    for tool in ['git','qwen','codex','claude','ffmpeg']:
        configured = os.environ.get('AO_' + tool.upper() + '_EXE') or tool
        exists = bool(shutil.which(configured) or Path(configured).is_file())
        print(f'{tool}: {"có" if exists else "không có"}; {"khuyến nghị" if tool == "git" else "không bắt buộc, tuỳ chọn theo worker"}')
    print('AO_ROOT:', ROOT)
    print('AO_MEMORY_DIR:', memory)
    print('AO_SECRETS_DIR:', os.environ.get('AO_SECRETS_DIR') or ROOT.parent / 'ao-secrets', '(ngoài repo, không tạo/đọc khóa)')
    env = dict(os.environ, PYTHONUTF8='1', AO_ROOT=str(ROOT))
    for script, action in [('queue.py','state'),('journal.py','verify')]:
        p = subprocess.run([sys.executable, str(ROOT/'tools/ao-journal'/script), action], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding='utf-8',
                           creationflags=0x08000000 if os.name=='nt' else 0)
        print(p.stdout, end='')
        if p.stderr: print(p.stderr, file=sys.stderr, end='')
        if p.returncode: return p.returncode
    print('SETUP_OK (giữ nguyên state đã có)')
    return 0

if __name__=='__main__': raise SystemExit(main())
